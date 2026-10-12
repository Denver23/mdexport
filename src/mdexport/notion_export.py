"""Stateless Notion export - one Markdown file per shared page, in a folder per root, re-reading only changed pages."""

import json
import re
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

# make_client is re-exported, so the CLI and tests patch this one name.
from mdexport.notion_client import NotionError, make_client
from mdexport.notion_render import FORMAT, compact_id, database_title, page_title, render_page
from mdexport.slack_export import _write_atomic

STAMP = "%Y-%m-%dT%H:%M:%SZ"
# Notion rounds last_edited_time to the minute, so an edit this close to the last read may have been missed.
REREAD_WINDOW = timedelta(seconds=60)

_ID = re.compile(r"[0-9a-f]{32}")
_INACCESSIBLE = ("object_not_found", "restricted_resource")
_PARENTS = {"page_id": "page", "database_id": "database", "block_id": "block"}
_OWN_PAGES = ("child_page", "child_database")


class ExportError(Exception):
    """The pages or databases shared with the integration could not be listed; nothing was written or deleted."""


def _inaccessible(e: NotionError) -> bool:
    return e.code in _INACCESSIBLE or e.status in (403, 404)


def _live(item: dict) -> bool:
    return not (item.get("archived") or item.get("in_trash"))


def _utc(stamp: str) -> datetime:
    moment = datetime.fromisoformat(stamp)
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


class _Shared:
    """The pages and databases shared with the integration, completed by the parents the root walks need."""

    def __init__(self, client, listed: dict[str, list[dict]]):
        self.client = client
        self.found: dict[str, dict[str, dict]] = {"page": {}, "database": {}, "block": {}}
        self.gone: set[str] = set()
        self.failed: dict[str, NotionError] = {}
        for kind, items in listed.items():
            self.found[kind].update((compact_id(item["id"]), item) for item in items if _live(item))

    def get(self, kind: str, item_id: str) -> dict | None:
        """The page, database or block, fetched once per run when not listed; None when inaccessible."""
        cache = self.found[kind]
        if item_id not in cache and item_id not in self.gone and item_id not in self.failed:
            try:
                item = getattr(self.client, kind)(item_id)
            except NotionError as e:
                if _inaccessible(e):
                    self.gone.add(item_id)
                else:
                    self.failed[item_id] = e
            else:
                if _live(item):
                    cache[item_id] = item
                else:
                    self.gone.add(item_id)
        if item_id in self.failed:
            raise self.failed[item_id]
        return cache.get(item_id)

    def chain(self, page_id: str) -> list[str]:
        """Page and database ids from the page's root down to the page; raises a parent's fetch error."""
        chain, seen = [page_id], {page_id}
        parent = self.found["page"][page_id].get("parent") or {}
        while kind := _PARENTS.get(parent.get("type")):
            item_id = compact_id(parent[parent["type"]])
            if item_id in seen:
                break  # a loop: the id seen again ends the chain
            seen.add(item_id)
            item = self.get(kind, item_id)
            if item is None:
                break
            if kind != "block":
                chain.append(item_id)
            parent = item.get("parent") or {}
        return chain[::-1]

    def title(self, item_id: str) -> str:
        page = self.found["page"].get(item_id)
        return page_title(page) if page is not None else database_title(self.found["database"][item_id])


def _kept_chains(shared: _Shared, excluded: set[str], unreadable: dict[str, str]) -> dict[str, list[str]]:
    """Each kept page's chain; pages whose walk fails go to `unreadable`, parents fetched on the way are walked too."""
    chains, walked = {}, set()
    while todo := [pid for pid in shared.found["page"] if pid not in walked]:
        for pid in todo:
            walked.add(pid)
            if pid in excluded:
                continue
            try:
                chain = shared.chain(pid)
            except NotionError as e:
                unreadable[pid] = e.code
                continue
            if excluded.isdisjoint(chain):
                chains[pid] = chain
    return chains


def _expand(client, blocks: list[dict]) -> list[dict]:
    """`blocks` with their children under `_children`; a synced copy takes its original's, none if unshared."""
    for block in blocks:
        kind = block.get("type")
        synced = block.get("synced_block") if kind == "synced_block" else None
        source = ((synced or {}).get("synced_from") or {}).get("block_id")
        if source:
            try:
                children = client.children(compact_id(source))
            except NotionError as e:
                if not _inaccessible(e):
                    raise
                children = []
            block["_children"] = _expand(client, children)
        elif block.get("has_children") and kind not in _OWN_PAGES:
            block["_children"] = _expand(client, client.children(compact_id(block["id"])))
    return blocks


def _state_path(out: Path, page_id: str) -> Path:
    return out / ".state" / "pages" / f"{page_id}.json"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _old_file(out: Path, page_id: str, state: dict) -> Path | None:
    """The page's file under the root its state names, if there is one."""
    root = state.get("root")
    path = out / root / f"{page_id}.md" if isinstance(root, str) and _ID.fullmatch(root) else None
    return path if path is not None and path.is_file() else None


def _stale(state: dict, edited: str, root: str, path: list[str]) -> bool:
    """Whether to read the page: no state, a changed edit time, root, path or format, or an edit close to the read."""
    if [state.get(key) for key in ("edited", "root", "path", "format")] != [edited, root, path, FORMAT]:
        return True
    return _utc(edited) >= _utc(state["fetched"]) - REREAD_WINDOW


def _write_page(out: Path, page_id: str, root: str, text: str, old: dict, new: dict) -> None:
    """Write the page file, then delete the one under its previous root, then its state; never delete first."""
    _write_atomic(out / root / f"{page_id}.md", text)
    old_root = old.get("root")
    if old_root != root and isinstance(old_root, str) and _ID.fullmatch(old_root):
        (out / old_root / f"{page_id}.md").unlink(missing_ok=True)
    _write_atomic(_state_path(out, page_id), json.dumps(new))


def _entries(folder: Path) -> list[Path]:
    return list(folder.iterdir()) if folder.is_dir() else []


def _id_named(path: Path, suffix: str) -> bool:
    return path.suffix == suffix and _ID.fullmatch(path.stem) is not None


def _on_disk(out: Path) -> tuple[list[Path], dict[str, list[Path]], set[str]]:
    """The id-named root folders, the id-named page files in them by page id, and the page ids with a state file."""
    folders = sorted(p for p in _entries(out) if _ID.fullmatch(p.name) and p.is_dir() and not p.is_symlink())
    files: dict[str, list[Path]] = {}
    for folder in folders:
        for path in _entries(folder):
            if _id_named(path, ".md") and path.is_file():
                files.setdefault(path.stem, []).append(path)
    states = {p.stem for p in _entries(out / ".state" / "pages") if _id_named(p, ".json") and p.is_file()}
    return folders, files, states


def _remove_gone(out: Path, shared: _Shared, chains: dict[str, list[str]], excluded: set[str],
                 unreadable: dict[str, str]) -> tuple[list[str], int, set[str]]:
    """Delete excluded and gone pages, then root folders left with no kept page; return roots, files, misses."""
    folders, files, states = _on_disk(out)
    walked, doomed, misses = set(shared.found["page"]), [], set()
    for pid in sorted((set(files) | states) - set(chains)):
        if pid in unreadable:
            continue
        # Walked but not kept means excluded; so does an excluded id or root folder, listed or not.
        if pid in walked or not excluded.isdisjoint({pid, *(path.parent.name for path in files.get(pid, ()))}):
            doomed.append(pid)
            continue
        try:
            item = shared.get("page", pid)
        except NotionError as e:
            unreadable[pid] = e.code
            continue
        if item is None:
            doomed.append(pid)
        else:
            misses.add(pid)  # still shared, only missed by the listing
    kept = {chain[0] for chain in chains.values()}
    kept |= {path.parent.name for pid in (*chains, *unreadable, *misses) for path in files.get(pid, ())}
    touched, pages_removed = set(), 0
    for pid in doomed:
        for path in files.get(pid, ()):
            path.unlink()
            touched.add(path.parent.name)
            pages_removed += 1
        _state_path(out, pid).unlink(missing_ok=True)
    removed = []
    for folder in folders:
        if folder.name in kept:
            continue
        if not any(folder.iterdir()):
            folder.rmdir()
        elif folder.name not in touched:
            continue  # only names that are not ours are left in it, and none of its pages went this run
        removed.append(folder.name)
    return removed, pages_removed, misses


def _count_file(report: dict, path: Path | None) -> None:
    """Count an existing page file and its `##` items in the report."""
    if path is not None:
        report["pages"] += 1
        report["sections"] += sum(line.startswith("## ") for line in path.read_text(encoding="utf-8").splitlines())


def export(client, out: Path, *, exclude: Iterable[str] = (), now: datetime | None = None) -> dict:
    """Export every page shared with the integration into `out/<root id>/<page id>.md` and return the run's report."""
    now = now or datetime.now(timezone.utc)
    try:
        listed = {kind: client.search(kind) for kind in ("page", "database")}
    except NotionError as e:
        raise ExportError(f"Notion listing failed: {e.code}") from e
    shared, unreadable, excluded = _Shared(client, listed), {}, {compact_id(item_id) for item_id in exclude}
    chains = _kept_chains(shared, excluded, unreadable)
    titles = {item_id: shared.title(item_id) for chain in chains.values() for item_id in chain}
    roots = {chain[0]: titles[chain[0]] for chain in chains.values()}
    fetched = now.astimezone(timezone.utc).strftime(STAMP)
    report = {"roots": 0, "pages": 0, "sections": 0, "read": 0, "removed": [], "pages_removed": 0,
              "unreadable": unreadable, "root_titles": {}}
    for pid, chain in chains.items():
        page, root, state = shared.found["page"][pid], chain[0], _read_json(_state_path(out, pid))
        path = [titles[item_id] for item_id in chain]
        if not _stale(state, page["last_edited_time"], root, path):
            _count_file(report, _old_file(out, pid, state))
            continue
        try:
            blocks = _expand(client, client.children(pid))
        except NotionError as e:
            # Nothing of this page has been written yet, so its file and state stay as they were.
            unreadable[pid] = e.code
            continue
        text, sections = render_page(page, blocks, path, titles[root], titles)
        new = {"root": root, "path": path, "edited": page["last_edited_time"], "fetched": fetched, "format": FORMAT}
        _write_page(out, pid, root, text, state, new)
        report["read"] += 1
        report["pages"] += 1
        report["sections"] += sections
    misses = set()
    if chains:
        report["removed"], report["pages_removed"], misses = _remove_gone(out, shared, chains, excluded, unreadable)
    else:
        report["removal_skipped"] = True  # a run that keeps no page never deletes anything
    previous = _read_json(out / ".state" / "roots.json")
    for pid in (*unreadable, *misses):
        # An unreadable or listing-missed page keeps its file, so the folder holding it stays a root this run.
        old = _old_file(out, pid, _read_json(_state_path(out, pid)))
        _count_file(report, old)
        if old is not None:
            roots.setdefault(old.parent.name, previous.get(old.parent.name) or old.parent.name)
    roots = dict(sorted(roots.items()))
    report["roots"], report["root_titles"] = len(roots), roots
    if chains:  # a run that keeps no page leaves the roots file as it was
        _write_atomic(out / ".state" / "roots.json", json.dumps(roots))
    return report
