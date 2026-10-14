"""Granola exporter - each meeting is written to the place it belongs to."""

from collections import Counter
from datetime import datetime
from pathlib import Path
import re
import time

import click
import httpx


API_BASE = "https://public-api.granola.ai/v1"
PUBLIC_MAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "icloud.com", "me.com", "yahoo.com", "proton.me", "protonmail.com", "yandex.ru", "mail.ru"}


def _client(token: str) -> httpx.Client:
    if token.startswith("eyJ"):
        from mdexport.granola_browser import BrowserClient
        return BrowserClient(token)
    return httpx.Client(base_url=API_BASE, headers={"Authorization": f"Bearer {token}"}, timeout=60)


def _get(client: httpx.Client, path: str, params: dict | None = None) -> httpx.Response:
    for attempt in range(5):
        resp = client.get(path, params=params)
        if resp.status_code != 429:
            break
        time.sleep(2 ** attempt)
    if resp.status_code == 401:
        raise click.ClickException("Granola did not accept the API key (401 Unauthorized).")
    return resp


def _pages(client: httpx.Client, path: str, key: str, params: dict) -> list[dict]:
    from mdexport.granola_browser import BrowserClient
    if isinstance(client, BrowserClient):
        return client.pages(path, params)
    items, cursor = [], None
    while True:
        resp = _get(client, path, {**params, "page_size": 30, **({"cursor": cursor} if cursor else {})})
        resp.raise_for_status()
        data = resp.json()
        items.extend(data[key])
        if not data.get("hasMore") or not data.get("cursor"):
            return items
        cursor = data["cursor"]


def list_folders(token: str) -> list[dict]:
    with _client(token) as client:
        return _pages(client, "/folders", "folders", {})


def _note(client: httpx.Client, note_id: str) -> dict | None:
    from mdexport.granola_browser import BrowserClient
    if isinstance(client, BrowserClient):
        return client.note(note_id)
    resp = _get(client, f"/notes/{note_id}", {"include": "transcript"})
    if resp.status_code == 404:
        return None
    if resp.status_code == 413:
        resp = _get(client, f"/notes/{note_id}")
        resp.raise_for_status()
        note = resp.json()
        tr = _get(client, f"/notes/{note_id}/transcript")
        tr.raise_for_status()
        body = tr.json()
        note["transcript"] = body.get("transcript", body) if isinstance(body, dict) else body
        return note
    resp.raise_for_status()
    return resp.json()


def series_of(note: dict) -> str | None:
    event_id = (note.get("calendar_event") or {}).get("calendar_event_id")
    return event_id.split("_")[0] if event_id else None


def _start(note: dict) -> datetime:
    when = (note.get("calendar_event") or {}).get("scheduled_start_time") or note["created_at"]
    return datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone()


def _people(note: dict) -> list[dict]:
    owner = (note.get("owner") or {}).get("email")
    seen, people = set(), []
    for p in note.get("attendees") or []:
        if p.get("email") and p["email"] != owner and p["email"] not in seen:
            seen.add(p["email"])
            people.append(p)
    for inv in (note.get("calendar_event") or {}).get("invitees") or []:
        if inv.get("email") and inv["email"] != owner and inv["email"] not in seen:
            seen.add(inv["email"])
            people.append({"name": None, "email": inv["email"]})
    return people


def _speaker(seg: dict) -> str:
    s = seg.get("speaker") or {}
    if s.get("name"):
        return s["name"]
    if s.get("attribution") == "me" or (not s.get("attribution") and s.get("source") == "microphone"):
        return "Me"
    return s.get("diarization_label") or "Them"


def to_markdown(note: dict) -> str:
    start = _start(note)
    event = note.get("calendar_event") or {}
    end = event.get("scheduled_end_time")
    when = start.strftime("%Y-%m-%d %H:%M")
    if end:
        when += "-" + datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    people = ", ".join(p.get("name") or p["email"] for p in _people(note))
    lines = [f"# {note.get('title') or event.get('event_title') or 'Untitled meeting'}", "", f"- Date: {when}"]
    if people:
        lines.append(f"- People: {people}")
    lines += [f"- Granola: {note['web_url']}", f"<!-- granola:{note['id']} -->", ""]
    summary = note.get("summary_markdown") or note.get("summary_text")
    if summary:
        lines += ["## Summary", "", summary.strip(), ""]
    mine = note.get("private_notes_markdown") or note.get("private_notes_text")
    if mine:
        lines += ["## My notes", "", mine.strip(), ""]
    transcript = note.get("transcript") or []
    if transcript:
        lines += ["## Transcript", ""]
        last = None
        for seg in transcript:
            who = _speaker(seg)
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            if who == last:
                lines[-2] += " " + text
                continue
            at = datetime.fromisoformat(seg["start_time"].replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S") if seg.get("start_time") else ""
            lines += [f"**{who}** ({at}): {text}" if at else f"**{who}**: {text}", ""]
            last = who
    return "\n".join(lines).rstrip() + "\n"


def _file_name(note: dict) -> str:
    title = note.get("title") or (note.get("calendar_event") or {}).get("event_title") or "Untitled meeting"
    title = re.sub(r'[\\/:*?"<>|\s]+', " ", title).strip()[:120] or "Untitled meeting"
    return f"{_start(note).strftime('%Y-%m-%d')} {title}"


def place_of(cfg: dict, note: dict) -> str | None:
    places = cfg.get("places") or {}
    for place in (
        (cfg.get("notes") or {}).get(note["id"]),
        (cfg.get("series") or {}).get(series_of(note) or ""),
        *[(cfg.get("folder_places") or {}).get(f["id"]) for f in note.get("folder_membership") or []],
    ):
        if place in places:
            return place
    return None


def guess(cfg: dict, emails: list[str]) -> str | None:
    places = cfg.get("places") or {}
    files = [f for f in (cfg.get("files") or {}).values() if f.get("place") in places]
    by_email = Counter(f["place"] for f in files for e in f.get("emails", []) if e in emails)
    if by_email:
        return by_email.most_common(1)[0][0]
    domains = {e.split("@")[-1].lower() for e in emails} - PUBLIC_MAIL
    by_domain = Counter(f["place"] for f in files for e in f.get("emails", []) if e.split("@")[-1].lower() in domains)
    return by_domain.most_common(1)[0][0] if by_domain else None


def _pending_item(note: dict) -> dict:
    return {
        "id": note["id"],
        "title": note.get("title") or (note.get("calendar_event") or {}).get("event_title") or "Untitled meeting",
        "start": _start(note).isoformat(),
        "people": [p.get("name") or p["email"] for p in _people(note)],
        "emails": [p["email"] for p in _people(note)],
        "series": series_of(note),
    }


def write(cfg: dict, note: dict, place: str) -> Path:
    files = cfg.setdefault("files", {})
    folder = Path(cfg["places"][place])
    folder.mkdir(parents=True, exist_ok=True)
    known = files.get(note["id"])
    old = Path(cfg["places"].get(known["place"], ""), known["file"]) if known and known.get("place") in cfg["places"] else None
    base = _file_name(note)
    if old and old.parent == folder and re.fullmatch(re.escape(base) + r"( \(\d+\))?", old.stem):
        target = old
    else:
        n = 1
        target = folder / f"{base}.md"
        while target.exists() and target != old:
            n += 1
            target = folder / f"{base} ({n}).md"
    text = to_markdown(note)
    if note.get("browser_session") and not note.get("transcript") and old and old.exists():
        previous = old.read_text()
        if "\n## Transcript\n" in previous:
            text += "\n## Transcript\n" + previous.split("\n## Transcript\n", 1)[1]
    target.write_text(text)
    if old and old != target and old.exists():
        old.unlink()
    files[note["id"]] = {"place": place, "file": target.name, "series": series_of(note), "emails": [p["email"] for p in _people(note)]}
    return target


def export_granola(token: str, cfg: dict, since: str | None = None) -> dict:
    """Write every changed meeting that has a place. Returns the updated cfg."""
    click.echo("Connecting to Granola...")
    params = {"updated_after": since} if since else {}
    if since:
        click.echo(f"Incremental sync since {since[:16].replace('T', ' ')}")
    pending = {p["id"]: p for p in cfg.get("pending") or []}
    written = 0
    with _client(token) as client:
        summaries = {}
        linked = list((cfg.get("folder_places") or {}).keys())
        folders = list(dict.fromkeys([*(cfg.get("folder_ids") or [None]), *linked]))
        for folder in folders:
            query = {} if folder in linked else params
            for n in _pages(client, "/notes", "notes", {**query, **({"folder_id": folder} if folder else {})}):
                summaries[n["id"]] = n
        live = [n for n in summaries.values() if not n.get("deleted_at")]
        click.echo(f"Found {len(live)} meeting(s) to look at")
        for i, summary in enumerate(live, 1):
            note = _note(client, summary["id"])
            if not note:
                continue
            click.echo(f"{i}/{len(live)} {_file_name(note)[11:]}")
            place = place_of(cfg, note)
            if not place:
                pending[note["id"]] = _pending_item(note)
                continue
            write(cfg, note, place)
            pending.pop(note["id"], None)
            sid = series_of(note)
            if sid:
                cfg.setdefault("series", {}).setdefault(sid, place)
            written += 1
    cfg["pending"] = sorted(pending.values(), key=lambda p: p["start"], reverse=True)
    click.echo(f"  Exported {written} meetings total")
    if cfg["pending"]:
        click.echo(f"  Waiting {len(cfg['pending'])} meetings to sort")
    return cfg


def file_note(token: str, cfg: dict, note_id: str, place: str, scope: str) -> list[str]:
    """Give a meeting a place and write it. scope is "auto" (sorting), "one" or "series". Returns the ids written."""
    with _client(token) as client:
        note = _note(client, note_id)
        if not note:
            raise click.ClickException("Granola has no finished notes for this meeting yet.")
        sid = series_of(note)
        if sid and scope != "one":
            cfg.setdefault("series", {})[sid] = place
            cfg.get("notes", {}).pop(note_id, None)
        else:
            cfg.setdefault("notes", {})[note_id] = place
        write(cfg, note, place)
        done = [note_id]
        if sid and scope == "series":
            for nid, f in list((cfg.get("files") or {}).items()):
                if nid != note_id and f.get("series") == sid:
                    cfg.get("notes", {}).pop(nid, None)
                    if f.get("place") != place:
                        _move_file(cfg, nid, place)
                        done.append(nid)
        if sid and scope != "one":
            for p in cfg.get("pending") or []:
                if p["id"] != note_id and p.get("series") == sid:
                    other = _note(client, p["id"])
                    if other:
                        write(cfg, other, place)
                        done.append(p["id"])
    cfg["pending"] = [p for p in cfg.get("pending") or [] if p["id"] not in done]
    return done


def _move_file(cfg: dict, note_id: str, place: str):
    f = cfg["files"][note_id]
    old = Path(cfg["places"].get(f["place"], ""), f["file"])
    folder = Path(cfg["places"][place])
    folder.mkdir(parents=True, exist_ok=True)
    stem, n = Path(f["file"]).stem, 1
    target = folder / f["file"]
    while target.exists():
        n += 1
        target = folder / f"{stem} ({n}).md"
    if old.exists():
        old.rename(target)
    f.update({"place": place, "file": target.name})
