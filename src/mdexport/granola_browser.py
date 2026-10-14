from datetime import datetime
import json

import click
import httpx


def _text(value):
    if isinstance(value, str):
        if value.lstrip().startswith("<"):
            from markdownify import markdownify
            return markdownify(value).strip()
        return value
    if isinstance(value, list):
        return "".join(_text(node) for node in value)
    if not isinstance(value, dict):
        return ""
    if value.get("text") is not None:
        text = value["text"]
        for mark in value.get("marks") or []:
            kind = mark.get("type")
            if kind == "bold":
                text = f"**{text}**"
            elif kind == "italic":
                text = f"*{text}*"
            elif kind == "link":
                text = f"[{text}]({mark.get('attrs', {}).get('href', '')})"
        return text
    for key in ("markdown", "markdown_text", "plain", "text"):
        if isinstance(value.get(key), str):
            return value[key]
    kind = value.get("type")
    text = _text(value.get("content") or value.get("children"))
    if kind == "heading":
        return "#" * min(6, max(1, value.get("attrs", {}).get("level", 2))) + " " + text.strip() + "\n\n"
    if kind == "listItem":
        return "- " + text.strip().replace("\n", "\n  ") + "\n"
    if kind in ("paragraph", "bulletList", "orderedList", "blockquote"):
        return text + "\n\n"
    if kind == "hardBreak":
        return "\n"
    return text


def _items(body, *keys):
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in keys:
            if isinstance(body.get(key), list):
                return body[key]
    raise click.ClickException("Granola returned an unexpected response. No existing files were removed.")


class BrowserClient(httpx.Client):
    def __init__(self, token, **kwargs):
        super().__init__(base_url="https://api.granola.ai", headers={"Authorization": f"Bearer {token}", "X-Client-Version": "7.0.0", "User-Agent": "NotulaCollect/0.2"}, timeout=60, **kwargs)
        self.documents = {}
        self.folder_cache = None
        self.viewer = None

    def read(self, endpoint, body, missing=False):
        import time
        for attempt in range(5):
            resp = self.post(endpoint, json=body)
            if resp.status_code != 429:
                break
            time.sleep(min(60, float(resp.headers.get("retry-after", 2 ** attempt))))
        if resp.status_code == 401:
            raise click.ClickException("Sign in to Granola again (401 Unauthorized).")
        if resp.status_code == 403:
            raise click.ClickException("Granola did not allow this account to read the requested content (403 Forbidden).")
        if resp.status_code == 404 and missing:
            return None
        resp.raise_for_status()
        return resp.json()

    def folders(self):
        if self.folder_cache is None:
            body = self.read("/v2/get-document-lists", {})
            self.folder_cache = _items(body, "document_lists", "lists")
        return self.folder_cache

    def pages(self, path, params):
        if path == "/folders":
            return [{"id": f["id"], "name": f.get("title") or f.get("name") or "Untitled folder"} for f in self.folders() if not f.get("deleted_at")]
        folder_id = params.get("folder_id")
        if folder_id:
            folder = next((f for f in self.folders() if f["id"] == folder_id), None)
            if folder is None:
                raise click.ClickException("The selected Granola folder is no longer available. Choose it again.")
            raw = folder.get("document_ids", folder.get("documents", []))
            ids = [d if isinstance(d, str) else d["id"] for d in raw]
            docs = []
            for start in range(0, len(ids), 100):
                body = self.read("/v1/get-documents-batch", {"document_ids": ids[start:start + 100], "include_last_viewed_panel": True})
                docs.extend(_items(body, "docs", "documents"))
        else:
            docs, seen, offset = [], set(), 0
            while True:
                body = self.read("/v2/get-documents", {"limit": 100, "offset": offset, "include_last_viewed_panel": True})
                page = _items(body, "docs", "documents")
                if page and not any(d["id"] not in seen for d in page):
                    raise click.ClickException("Granola repeated a page of meetings. Try again later.")
                docs.extend(d for d in page if d["id"] not in seen)
                seen.update(d["id"] for d in page)
                if len(page) < 100:
                    break
                offset += len(page)
        self.documents.update({d["id"]: d for d in docs})
        since = params.get("updated_after")
        return [d for d in docs if not d.get("deleted_at") and (not since or not d.get("updated_at") or datetime.fromisoformat(d["updated_at"].replace("Z", "+00:00")) >= datetime.fromisoformat(since.replace("Z", "+00:00")))]

    def speaker(self, chunk, doc):
        speaker = {"source": chunk.get("source"), **(chunk.get("speaker") or {})}
        detected = chunk.get("detectedSpeaker") or {}
        name = speaker.get("name") or detected.get("participantName") or chunk.get("detected_speaker_name")
        recorder = chunk.get("transcriber_user_id") or doc.get("user_id")
        if not name and speaker.get("source") == "microphone" and recorder:
            if self.viewer is None:
                self.viewer = self.read("/v1/get-user-info", {})
            if recorder == self.viewer.get("id"):
                name = (self.viewer.get("person") or {}).get("name")
        if name:
            speaker["name"] = name
        return speaker

    def note(self, note_id):
        doc = self.documents.get(note_id)
        if doc is None:
            body = self.read("/v1/get-documents-batch", {"document_ids": [note_id], "include_last_viewed_panel": True})
            doc = next((d for d in _items(body, "docs", "documents") if d["id"] == note_id), None)
        if not doc or doc.get("deleted_at"):
            return None
        panel = doc.get("last_viewed_panel") or {}
        summary = _text(panel.get("content") or doc.get("summary")).strip()
        if not summary:
            panels = _items(self.read("/v1/get-document-panels", {"document_id": note_id}), "panels")
            live = [p for p in panels if not p.get("deleted_at")]
            live.sort(key=lambda p: p.get("updated_at") or p.get("created_at") or "", reverse=True)
            summary = _text(live[0].get("content")).strip() if live else ""
        if not summary:
            return None
        event = doc.get("google_calendar_event") or {}
        if isinstance(event, str):
            event = json.loads(event)
        people = doc.get("people") or {}
        attendees = people if isinstance(people, list) else people.get("attendees", [])
        attendees = [{"email": p["email"], "name": p.get("name") or p.get("displayName")} for p in [*attendees, *(event.get("attendees") or [])] if p.get("email")]
        creator = (people.get("creator") or {}) if isinstance(people, dict) else {}
        transcript = self.read("/v1/get-document-transcript", {"document_id": note_id}, missing=True)
        chunks = [] if transcript is None else _items(transcript, "transcript")
        return {
            "id": note_id,
            "browser_session": True,
            "title": doc.get("title"),
            "created_at": doc["created_at"],
            "web_url": f"https://notes.granola.ai/d/{note_id}",
            "calendar_event": {"calendar_event_id": event.get("recurringEventId") or event.get("id"), "event_title": event.get("summary") or event.get("title"), "scheduled_start_time": (event.get("start") or {}).get("dateTime"), "scheduled_end_time": (event.get("end") or {}).get("dateTime"), "invitees": attendees},
            "owner": creator,
            "attendees": attendees,
            "folder_membership": [{"id": f["id"]} for f in self.folders() if note_id in [d if isinstance(d, str) else d["id"] for d in f.get("document_ids", f.get("documents", []))]],
            "summary_markdown": summary,
            "private_notes_markdown": doc.get("notes_markdown") or doc.get("notes_plain") or _text(doc.get("notes")).strip(),
            "transcript": [{"text": c.get("text", ""), "start_time": c.get("start_timestamp"), "speaker": self.speaker(c, doc)} for c in chunks],
        }
