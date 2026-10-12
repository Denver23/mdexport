import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mdexport import notion_client, notion_export, slack_export
from mdexport.notion_client import NotionError
from mdexport.notion_export import ExportError, export

from .fake_notion import FakeNotion

NOW = datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(days=1)
EDITED = "2026-10-08T09:12:00.000Z"
NEW_EDIT = "2026-10-09T08:00:00.000Z"
WORKSPACE = {"type": "workspace", "workspace": True}


def nid(tag: str) -> str:
    """A compact id repeating the two hex digits `tag`."""
    return tag * 16


R, R1, R2, P, Q, D, X, Y, A, B, T = map(nid, ("a0", "a1", "a2", "b0", "b1", "d0", "c1", "c2", "f0", "e0", "e1"))


def dashed(compact: str) -> str:
    return f"{compact[:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:]}"


def under(kind: str, parent_id: str) -> dict:
    """A `parent` of `kind` (`page_id`, `database_id` or `block_id`), dashed as Notion returns it."""
    return {"type": kind, kind: dashed(parent_id)}


def rt(text: str) -> list[dict]:
    return [{"plain_text": text, "href": None}]


def block(kind: str, block_id: str, data: dict, has_children: bool = False, **extra) -> dict:
    return {"object": "block", "id": dashed(block_id), "type": kind, "has_children": has_children, kind: data, **extra}


def para(text: str) -> dict:
    return block("paragraph", nid("99"), {"rich_text": rt(text)})


def heading(text: str) -> dict:
    return block("heading_2", nid("98"), {"rich_text": rt(text), "is_toggleable": False})


def add_page(fake: FakeNotion, pid: str, title: str, parent: dict = WORKSPACE, blocks: list | None = None,
             edited: str = EDITED, **extra) -> dict:
    """Add a page and its top-level blocks, by default one paragraph."""
    fake.pages[pid] = {
        "object": "page", "id": dashed(pid), "url": f"https://www.notion.so/{pid}", "last_edited_time": edited,
        "parent": parent, "archived": False, "in_trash": False,
        "properties": {"Name": {"type": "title", "title": rt(title)}}, **extra,
    }
    fake.children[pid] = [para(f"{title} body")] if blocks is None else blocks
    return fake.pages[pid]


def add_database(fake: FakeNotion, did: str, title: str, parent: dict = WORKSPACE) -> None:
    fake.databases[did] = {"object": "database", "id": dashed(did), "title": rt(title), "parent": parent,
                           "archived": False, "in_trash": False}


def root_and_child() -> FakeNotion:
    fake = FakeNotion()
    add_page(fake, R, "R title")
    add_page(fake, P, "P title", under("page_id", R), [para("intro"), heading("Plan")])
    return fake


def run(fake: FakeNotion, out: Path, now: datetime = NOW, **kwargs) -> dict:
    return export(fake, out, now=now, **kwargs)


def md_files(out: Path) -> list[str]:
    return sorted(p.relative_to(out).as_posix() for p in out.rglob("*.md"))


def tree(out: Path) -> dict[str, bytes]:
    return {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()}


def state_path(out: Path, pid: str) -> Path:
    return out / ".state" / "pages" / f"{pid}.json"


def state(out: Path, pid: str) -> dict:
    return json.loads(state_path(out, pid).read_text())


def patch_state(out: Path, pid: str, **changes) -> None:
    state_path(out, pid).write_text(json.dumps({**state(out, pid), **changes}))


def roots_json(out: Path) -> dict:
    return json.loads((out / ".state" / "roots.json").read_text())


def children_calls(fake: FakeNotion) -> list[str]:
    return [item_id for method, item_id in fake.calls if method == "children"]


def test_make_client_is_the_clients():
    assert notion_export.make_client is notion_client.make_client


def test_first_run_writes_root_folder_and_state(tmp_path):
    report = run(root_and_child(), tmp_path)
    assert md_files(tmp_path) == [f"{R}/{R}.md", f"{R}/{P}.md"]
    assert (tmp_path / R / f"{P}.md").read_text().startswith(
        "# P title\nroot: R title · path: R title › P title · edited 2026-10-08\n\n## P title\n"
        f"https://www.notion.so/{P}\nintro\n\n## P title › Plan\n"
    )
    assert state(tmp_path, P) == {"root": R, "path": ["R title", "P title"], "edited": EDITED,
                                  "fetched": "2026-10-09T03:00:00Z", "format": 1}
    assert state(tmp_path, R)["path"] == ["R title"]
    assert roots_json(tmp_path) == {R: "R title"}
    assert report == {"roots": 1, "pages": 2, "sections": 3, "read": 2, "removed": [], "pages_removed": 0,
                      "unreadable": {}, "root_titles": {R: "R title"}}


def test_database_rows_rooted_at_shared_database(tmp_path):
    fake = FakeNotion()
    add_database(fake, D, "Clients")
    add_page(fake, X, "Seed", under("database_id", D))
    add_page(fake, Y, "Acme", under("database_id", D))
    report = run(fake, tmp_path)
    assert md_files(tmp_path) == [f"{D}/{X}.md", f"{D}/{Y}.md"]
    assert (tmp_path / D / f"{Y}.md").read_text().startswith("# Acme\nroot: Clients · path: Clients › Acme · ")
    assert state(tmp_path, X)["path"] == ["Clients", "Seed"]
    assert report["root_titles"] == {D: "Clients"}
    assert roots_json(tmp_path) == {D: "Clients"}
    assert (report["roots"], report["pages"], report["read"]) == (1, 2, 2)


@pytest.mark.parametrize("error", [None, NotionError(403, "restricted_resource")])
def test_unshared_parent_makes_page_its_own_root(tmp_path, error):
    fake = FakeNotion()
    add_page(fake, P, "P title", under("page_id", R))
    if error:
        fake.fail[("page", R)] = error
    report = run(fake, tmp_path)
    assert md_files(tmp_path) == [f"{P}/{P}.md"]
    assert state(tmp_path, P)["root"] == P
    assert state(tmp_path, P)["path"] == ["P title"]
    assert fake.calls.count(("page", R)) == 1
    assert report["unreadable"] == {}
    assert report["root_titles"] == {P: "P title"}


def test_parent_missed_by_listing_is_fetched(tmp_path):
    fake = FakeNotion()
    add_page(fake, R, "R title")
    add_page(fake, P, "P title", under("page_id", R))
    add_page(fake, Q, "Q title", under("page_id", R))
    fake.listing["page"] = [P, Q]
    report = run(fake, tmp_path)
    assert state(tmp_path, P)["root"] == R
    assert state(tmp_path, Q)["path"] == ["R title", "Q title"]
    assert md_files(tmp_path) == [f"{R}/{R}.md", f"{R}/{P}.md", f"{R}/{Q}.md"]
    assert fake.calls.count(("page", R)) == 1
    assert report["root_titles"] == {R: "R title"}


def test_block_parent_resolved(tmp_path):
    fake = FakeNotion()
    add_page(fake, R, "R title")
    fake.blocks[T] = block("column_list", T, {}, True, parent=under("page_id", R))
    fake.blocks[B] = block("column", B, {}, True, parent=under("block_id", T))
    add_page(fake, P, "P title", under("block_id", B))
    run(fake, tmp_path)
    assert state(tmp_path, P)["root"] == R
    assert state(tmp_path, P)["path"] == ["R title", "P title"]
    assert md_files(tmp_path) == [f"{R}/{R}.md", f"{R}/{P}.md"]
    assert (fake.calls.count(("block", B)), fake.calls.count(("block", T))) == (1, 1)


def test_cycle_stops(tmp_path):
    fake = FakeNotion()
    add_page(fake, P, "P title", under("page_id", Q))
    add_page(fake, Q, "Q title", under("page_id", P))
    report = run(fake, tmp_path)
    assert md_files(tmp_path) == [f"{P}/{Q}.md", f"{Q}/{P}.md"]
    assert state(tmp_path, P)["path"] == ["Q title", "P title"]
    assert children_calls(fake) == [P, Q]
    assert (report["roots"], report["pages"], report["read"]) == (2, 2, 2)


def test_block_cycle_stops(tmp_path):
    fake = FakeNotion()
    fake.blocks[T] = block("column_list", T, {}, True, parent=under("block_id", B))
    fake.blocks[B] = block("column", B, {}, True, parent=under("block_id", T))
    add_page(fake, P, "P title", under("block_id", B))
    run(fake, tmp_path)
    assert md_files(tmp_path) == [f"{P}/{P}.md"]
    assert (fake.calls.count(("block", B)), fake.calls.count(("block", T))) == (1, 1)


def test_deep_block_chain_reaches_root(tmp_path):
    fake = FakeNotion()
    add_page(fake, R, "R title")
    ids = [f"{n:032x}" for n in range(1, 1501)]
    for here, up in zip(ids, ids[1:]):
        fake.blocks[here] = block("column", here, {}, True, parent=under("block_id", up))
    fake.blocks[ids[-1]] = block("column", ids[-1], {}, True, parent=under("page_id", R))
    add_page(fake, P, "P title", under("block_id", ids[0]))
    run(fake, tmp_path)
    assert state(tmp_path, P)["root"] == R


def test_excluded_subtree_dropped(tmp_path):
    fake = root_and_child()
    add_page(fake, Q, "Q title", under("page_id", P))
    report = run(fake, tmp_path, exclude=[dashed(R).upper()])
    assert md_files(tmp_path) == []
    assert children_calls(fake) == []
    assert not (tmp_path / ".state" / "roots.json").exists()
    assert report == {"roots": 0, "pages": 0, "sections": 0, "read": 0, "removed": [], "pages_removed": 0,
                      "unreadable": {}, "root_titles": {}, "removal_skipped": True}


def test_excluded_database_and_page_dropped(tmp_path):
    fake = root_and_child()
    add_database(fake, D, "Tasks", under("page_id", R))
    add_page(fake, X, "Task", under("database_id", D))
    add_page(fake, Q, "Q title", under("page_id", R))
    report = run(fake, tmp_path, exclude=[dashed(D).upper(), P.upper()])
    assert md_files(tmp_path) == [f"{R}/{R}.md", f"{R}/{Q}.md"]
    assert sorted(children_calls(fake)) == [R, Q]
    assert report["pages"] == 2


def test_unchanged_page_not_read(tmp_path):
    fake = root_and_child()
    run(fake, tmp_path)
    before = tree(tmp_path)
    fake.calls.clear()
    report = run(fake, tmp_path, now=LATER)
    assert children_calls(fake) == []
    assert tree(tmp_path) == before
    assert report == {"roots": 1, "pages": 2, "sections": 3, "read": 0, "removed": [], "pages_removed": 0,
                      "unreadable": {}, "root_titles": {R: "R title"}}


def test_edited_page_read(tmp_path):
    fake = root_and_child()
    run(fake, tmp_path)
    fake.pages[P]["last_edited_time"] = NEW_EDIT
    fake.children[P] = [para("rewritten")]
    fake.calls.clear()
    report = run(fake, tmp_path, now=LATER)
    assert children_calls(fake) == [P]
    text = (tmp_path / R / f"{P}.md").read_text()
    assert "edited 2026-10-09\n" in text
    assert "rewritten" in text
    assert "intro" not in text
    assert state(tmp_path, P)["edited"] == NEW_EDIT
    assert state(tmp_path, P)["fetched"] == "2026-10-10T03:00:00Z"
    assert (report["read"], report["sections"]) == (1, 2)


@pytest.mark.parametrize("fetched, read", [
    ("2026-10-09T03:00:20Z", 1),
    ("2026-10-09T03:01:00Z", 1),
    ("2026-10-09T03:01:01Z", 0),
    ("2026-10-09T03:05:00Z", 0),
])
def test_edit_within_a_minute_read_again(tmp_path, fetched, read):
    fake = FakeNotion()
    add_page(fake, P, "P title", edited="2026-10-09T03:00:00.000Z")
    run(fake, tmp_path)
    patch_state(tmp_path, P, fetched=fetched)
    fake.calls.clear()
    assert run(fake, tmp_path, now=LATER)["read"] == read
    assert children_calls(fake) == [P] * read


def test_path_change_rereads(tmp_path):
    fake = root_and_child()
    run(fake, tmp_path)
    fake.pages[R]["properties"]["Name"]["title"] = rt("Clients")
    fake.calls.clear()
    report = run(fake, tmp_path, now=LATER)
    assert children_calls(fake) == [R, P]
    text = (tmp_path / R / f"{P}.md").read_text()
    assert "\nroot: Clients · path: Clients › P title · edited 2026-10-08\n" in text
    assert state(tmp_path, P)["path"] == ["Clients", "P title"]
    assert report["root_titles"] == {R: "Clients"}
    assert roots_json(tmp_path) == {R: "Clients"}


def test_format_change_rereads(tmp_path):
    fake = FakeNotion()
    add_page(fake, P, "P title")
    run(fake, tmp_path)
    patch_state(tmp_path, P, format=0)
    fake.calls.clear()
    assert run(fake, tmp_path, now=LATER)["read"] == 1
    assert children_calls(fake) == [P]
    assert state(tmp_path, P)["format"] == 1


def moved_page() -> FakeNotion:
    """P under R1, ready to move under R2."""
    fake = FakeNotion()
    add_page(fake, R1, "R1 title")
    add_page(fake, R2, "R2 title")
    add_page(fake, P, "P title", under("page_id", R1))
    return fake


def test_root_change_moves_file(tmp_path, monkeypatch):
    fake = moved_page()
    run(fake, tmp_path)
    fake.pages[P]["parent"] = under("page_id", R2)
    events, real_replace, real_unlink = [], os.replace, Path.unlink

    def replace(src, dst):
        events.append(("write", Path(dst)))
        real_replace(src, dst)

    def unlink(path, missing_ok=False):
        events.append(("delete", Path(path)))
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(slack_export.os, "replace", replace)
    monkeypatch.setattr(Path, "unlink", unlink)
    report = run(fake, tmp_path, now=LATER)
    assert md_files(tmp_path) == [f"{R1}/{R1}.md", f"{R2}/{R2}.md", f"{R2}/{P}.md"]
    assert state(tmp_path, P)["root"] == R2
    assert state(tmp_path, P)["path"] == ["R2 title", "P title"]
    assert [(kind, path.relative_to(tmp_path).as_posix()) for kind, path in events] == [
        ("write", f"{R2}/{P}.md"),
        ("delete", f"{R1}/{P}.md"),
        ("write", f".state/pages/{P}.json"),
        ("write", ".state/roots.json"),
    ]
    assert report["read"] == 1


def test_failed_write_deletes_nothing(tmp_path, monkeypatch):
    fake = moved_page()
    run(fake, tmp_path)
    fake.pages[P]["parent"] = under("page_id", R2)
    before = tree(tmp_path)

    def crash(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(slack_export.os, "replace", crash)
    with pytest.raises(OSError):
        run(fake, tmp_path, now=LATER)
    assert tree(tmp_path) == before


def test_failed_read_keeps_old_file_and_state(tmp_path):
    fake = moved_page()
    add_page(fake, Q, "Q title", under("page_id", R2))
    run(fake, tmp_path)
    old_file, old_state = (tmp_path / R1 / f"{P}.md").read_bytes(), state_path(tmp_path, P).read_bytes()
    fake.pages[P]["parent"] = under("page_id", R2)
    fake.fail[("children", P)] = NotionError(500, "server_error")
    fake.pages[Q]["last_edited_time"] = NEW_EDIT
    fake.children[Q] = [para("Q news")]
    report = run(fake, tmp_path, now=LATER)
    assert (tmp_path / R1 / f"{P}.md").read_bytes() == old_file
    assert state_path(tmp_path, P).read_bytes() == old_state
    assert not (tmp_path / R2 / f"{P}.md").exists()
    assert "Q news" in (tmp_path / R2 / f"{Q}.md").read_text()
    assert report == {"roots": 2, "pages": 4, "sections": 4, "read": 1, "removed": [], "pages_removed": 0,
                      "unreadable": {P: "server_error"}, "root_titles": {R1: "R1 title", R2: "R2 title"}}


def test_parent_fetch_error_makes_page_unreadable(tmp_path):
    fake = FakeNotion()
    add_page(fake, R, "R title")
    fake.blocks[B] = block("column", B, {}, True, parent=under("page_id", R))
    add_page(fake, P, "P title", under("block_id", B))
    add_page(fake, Q, "Q title", under("block_id", B))
    run(fake, tmp_path)
    before = tree(tmp_path)
    fake.fail[("block", B)] = NotionError(500, "server_error")
    fake.pages[P]["last_edited_time"] = NEW_EDIT
    fake.calls.clear()
    report = run(fake, tmp_path, now=LATER)
    assert report["unreadable"] == {P: "server_error", Q: "server_error"}
    assert fake.calls.count(("block", B)) == 1
    assert children_calls(fake) == []
    assert tree(tmp_path) == before
    assert report == {"roots": 1, "pages": 3, "sections": 3, "read": 0, "removed": [], "pages_removed": 0,
                      "unreadable": {P: "server_error", Q: "server_error"}, "root_titles": {R: "R title"}}


def test_walk_failed_page_keeps_its_own_root(tmp_path):
    fake = FakeNotion()
    fake.blocks[B] = block("column", B, {}, True, parent=under("page_id", R))
    add_page(fake, P, "P title", under("block_id", B))
    add_page(fake, X, "X title")
    run(fake, tmp_path)
    assert roots_json(tmp_path) == {P: "P title", X: "X title"}
    before = tree(tmp_path)
    fake.fail[("block", B)] = NotionError(500, "server_error")
    report = run(fake, tmp_path, now=LATER)
    assert tree(tmp_path) == before
    assert report == {"roots": 2, "pages": 2, "sections": 2, "read": 0, "removed": [], "pages_removed": 0,
                      "unreadable": {P: "server_error"}, "root_titles": {P: "P title", X: "X title"}}


def test_unreadable_page_keeps_its_old_root(tmp_path):
    fake = FakeNotion()
    add_database(fake, R1, "R1 title")
    add_page(fake, R2, "R2 title")
    add_page(fake, P, "P title", under("database_id", R1))
    run(fake, tmp_path)
    old_file = (tmp_path / R1 / f"{P}.md").read_bytes()
    fake.pages[P]["parent"] = under("page_id", R2)
    fake.fail[("children", P)] = NotionError(500, "server_error")
    report = run(fake, tmp_path, now=LATER)
    assert (tmp_path / R1 / f"{P}.md").read_bytes() == old_file
    assert md_files(tmp_path) == [f"{R1}/{P}.md", f"{R2}/{R2}.md"]
    assert roots_json(tmp_path) == {R1: "R1 title", R2: "R2 title"}
    assert report == {"roots": 2, "pages": 2, "sections": 2, "read": 0, "removed": [], "pages_removed": 0,
                      "unreadable": {P: "server_error"}, "root_titles": {R1: "R1 title", R2: "R2 title"}}
    (tmp_path / ".state" / "roots.json").unlink()
    assert run(fake, tmp_path, now=LATER)["root_titles"] == {R1: R1, R2: "R2 title"}
    assert roots_json(tmp_path) == {R1: R1, R2: "R2 title"}


@pytest.mark.parametrize("change", ["unshared", "excluded"])
def test_nothing_kept_leaves_roots_json(tmp_path, change):
    fake = root_and_child()
    run(fake, tmp_path)
    before = (tmp_path / ".state" / "roots.json").read_bytes()
    if change == "unshared":
        fake.listing = {"page": [], "database": []}
    report = run(fake, tmp_path, now=LATER, exclude=[R] if change == "excluded" else [])
    assert (tmp_path / ".state" / "roots.json").read_bytes() == before
    assert (report["roots"], report["pages"], report["root_titles"]) == (0, 0, {})


def test_nested_children_read_but_not_child_pages(tmp_path):
    fake = FakeNotion()
    toggle = block("toggle", T, {"rich_text": rt("More")}, True)
    original = block("synced_block", B, {"synced_from": None}, True)
    sub_page = block("child_page", Q, {"title": "Q title"}, True)
    sub_database = block("child_database", D, {"title": "Tasks"}, True)
    add_page(fake, P, "P title", blocks=[toggle, original, sub_page, sub_database])
    fake.children[T] = [para("inside toggle")]
    fake.children[B] = [para("original words")]
    run(fake, tmp_path)
    assert children_calls(fake) == [P, T, B]
    assert (tmp_path / P / f"{P}.md").read_text().endswith(
        "▸ More\n  inside toggle\noriginal words\nSub-page: Q title\nDatabase: Tasks\n"
    )


def synced_page() -> FakeNotion:
    """P holding a copy of the synced block A."""
    fake = FakeNotion()
    copy = block("synced_block", B, {"synced_from": {"type": "block_id", "block_id": dashed(A)}}, True)
    add_page(fake, P, "P title", blocks=[para("own words"), copy])
    fake.children[A] = [para("shared words")]
    return fake


def test_synced_block_from_original(tmp_path):
    fake = synced_page()
    run(fake, tmp_path)
    assert children_calls(fake) == [P, A]
    assert (tmp_path / P / f"{P}.md").read_text().endswith("own words\nshared words\n")
    del fake.children[A]
    fake.pages[P]["last_edited_time"] = NEW_EDIT
    report = run(fake, tmp_path, now=LATER)
    assert (tmp_path / P / f"{P}.md").read_text().endswith("own words\n")
    assert (report["read"], report["unreadable"]) == (1, {})


def test_synced_original_failing_makes_page_unreadable(tmp_path):
    fake = synced_page()
    fake.fail[("children", A)] = NotionError(500, "server_error")
    report = run(fake, tmp_path)
    assert report["unreadable"] == {P: "server_error"}
    assert md_files(tmp_path) == []
    assert not state_path(tmp_path, P).exists()


@pytest.mark.parametrize("kind", ["page", "database"])
def test_listing_failure_changes_nothing(tmp_path, kind):
    fake = root_and_child()
    out = tmp_path / "out"
    run(fake, out)
    fake.pages[P]["last_edited_time"] = NEW_EDIT
    before = tree(out)
    fake.list_fail[kind] = NotionError(401, "unauthorized")
    fake.calls.clear()
    with pytest.raises(ExportError) as raised:
        run(fake, out, now=LATER)
    assert str(raised.value) == "Notion listing failed: unauthorized"
    assert tree(out) == before
    assert {method for method, _ in fake.calls} == {"search"}
    with pytest.raises(ExportError):
        run(fake, tmp_path / "fresh")
    assert not (tmp_path / "fresh").exists()


def test_archived_and_trashed_ignored(tmp_path):
    fake = FakeNotion()
    add_page(fake, R, "Old", archived=True)
    add_page(fake, Q, "Binned", in_trash=True)
    add_page(fake, P, "P title", under("page_id", R))
    report = run(fake, tmp_path)
    assert md_files(tmp_path) == [f"{P}/{P}.md"]
    assert fake.calls.count(("page", R)) == 1
    assert children_calls(fake) == [P]
    assert (report["roots"], report["pages"]) == (1, 1)


def test_temp_files_end_in_tmp(tmp_path, monkeypatch):
    fake = FakeNotion()
    add_page(fake, R, "R title")
    real, seen = os.replace, []

    def spy(src, dst):
        seen.append((Path(src), Path(dst)))
        real(src, dst)

    monkeypatch.setattr(slack_export.os, "replace", spy)
    run(fake, tmp_path)
    assert [dst.relative_to(tmp_path).as_posix() for _, dst in seen] == [
        f"{R}/{R}.md", f".state/pages/{R}.json", ".state/roots.json"
    ]
    for src, dst in seen:
        assert src.parent == dst.parent
        assert src.name.endswith(".tmp")


def page_calls(fake: FakeNotion) -> list[str]:
    return [item_id for method, item_id in fake.calls if method == "page"]


def two_roots() -> FakeNotion:
    """Roots R1 and R2, with P under R2."""
    fake = FakeNotion()
    add_page(fake, R1, "R1 title")
    add_page(fake, R2, "R2 title")
    add_page(fake, P, "P title", under("page_id", R2))
    return fake


@pytest.mark.parametrize("error", [NotionError(404, "object_not_found"), NotionError(403, "restricted_resource")])
def test_unshared_page_deleted_after_check(tmp_path, error):
    fake = root_and_child()
    run(fake, tmp_path)
    rest = {path: data for path, data in tree(tmp_path).items() if P not in path}
    fake.listing["page"] = [R]
    fake.fail[("page", P)] = error
    fake.calls.clear()
    report = run(fake, tmp_path, now=LATER)
    assert tree(tmp_path) == rest
    assert page_calls(fake) == [P]
    assert report == {"roots": 1, "pages": 1, "sections": 1, "read": 0, "removed": [], "pages_removed": 1,
                      "unreadable": {}, "root_titles": {R: "R title"}}


@pytest.mark.parametrize("flag", ["archived", "in_trash"])
def test_trashed_page_deleted(tmp_path, flag):
    fake = root_and_child()
    run(fake, tmp_path)
    fake.listing["page"] = [R]
    fake.pages[P][flag] = True
    fake.calls.clear()
    report = run(fake, tmp_path, now=LATER)
    assert md_files(tmp_path) == [f"{R}/{R}.md"]
    assert not state_path(tmp_path, P).exists()
    assert page_calls(fake) == [P]
    assert (report["removed"], report["pages_removed"]) == ([], 1)


def test_listing_miss_kept(tmp_path):
    fake = root_and_child()
    run(fake, tmp_path)
    before = tree(tmp_path)
    fake.listing["page"] = [R]
    fake.calls.clear()
    report = run(fake, tmp_path, now=LATER)
    assert tree(tmp_path) == before
    assert page_calls(fake) == [P]
    assert children_calls(fake) == []
    assert report == {"roots": 1, "pages": 2, "sections": 3, "read": 0, "removed": [], "pages_removed": 0,
                      "unreadable": {}, "root_titles": {R: "R title"}}


def test_check_error_keeps_and_reports(tmp_path):
    fake = root_and_child()
    run(fake, tmp_path)
    before = tree(tmp_path)
    fake.listing["page"] = [R]
    fake.fail[("page", P)] = NotionError(500, "server_error")
    report = run(fake, tmp_path, now=LATER)
    assert tree(tmp_path) == before
    assert report == {"roots": 1, "pages": 2, "sections": 3, "read": 0, "removed": [], "pages_removed": 0,
                      "unreadable": {P: "server_error"}, "root_titles": {R: "R title"}}


@pytest.mark.parametrize("listed", [True, False])
def test_excluded_page_deleted_without_check(tmp_path, listed):
    fake = root_and_child()
    run(fake, tmp_path)
    if not listed:
        fake.listing["page"] = [R]
    fake.calls.clear()
    report = run(fake, tmp_path, now=LATER, exclude=[dashed(P).upper()])
    assert md_files(tmp_path) == [f"{R}/{R}.md"]
    assert not state_path(tmp_path, P).exists()
    assert page_calls(fake) == []
    assert (report["removed"], report["pages_removed"]) == ([], 1)


def test_page_under_excluded_page_deleted(tmp_path):
    fake = root_and_child()
    add_page(fake, Q, "Q title", under("page_id", P))
    run(fake, tmp_path)
    fake.calls.clear()
    report = run(fake, tmp_path, now=LATER, exclude=[P])
    assert md_files(tmp_path) == [f"{R}/{R}.md"]
    assert not state_path(tmp_path, Q).exists()
    assert page_calls(fake) == []
    assert (report["removed"], report["pages_removed"]) == ([], 2)


def test_excluded_root_keeps_nothing(tmp_path):
    fake = two_roots()
    run(fake, tmp_path)
    fake.listing["page"] = [R1, R2]
    fake.calls.clear()
    report = run(fake, tmp_path, now=LATER, exclude=[R2])
    assert md_files(tmp_path) == [f"{R1}/{R1}.md"]
    assert not (tmp_path / R2).exists()
    assert not state_path(tmp_path, R2).exists()
    assert not state_path(tmp_path, P).exists()
    assert page_calls(fake) == []
    assert report == {"roots": 1, "pages": 1, "sections": 1, "read": 0, "removed": [R2], "pages_removed": 2,
                      "unreadable": {}, "root_titles": {R1: "R1 title"}}


def test_unshared_root_folder_deleted(tmp_path):
    fake = two_roots()
    run(fake, tmp_path)
    before = tree(tmp_path)
    del fake.pages[R2], fake.pages[P]
    fake.calls.clear()
    report = run(fake, tmp_path, now=LATER)
    assert {k: v for k, v in tree(tmp_path).items() if k != ".state/roots.json"} == {
        k: v for k, v in before.items() if R1 in k}
    assert not (tmp_path / R2).exists()
    assert sorted(page_calls(fake)) == sorted([R2, P])
    assert roots_json(tmp_path) == {R1: "R1 title"}
    assert report == {"roots": 1, "pages": 1, "sections": 1, "read": 0, "removed": [R2], "pages_removed": 2,
                      "unreadable": {}, "root_titles": {R1: "R1 title"}}


def test_unshared_database_folder_deleted(tmp_path):
    fake = FakeNotion()
    add_page(fake, R1, "R1 title")
    add_database(fake, D, "Clients")
    add_page(fake, X, "Seed", under("database_id", D))
    run(fake, tmp_path)
    del fake.databases[D], fake.pages[X]
    fake.calls.clear()
    report = run(fake, tmp_path, now=LATER)
    assert md_files(tmp_path) == [f"{R1}/{R1}.md"]
    assert not (tmp_path / D).exists()
    assert not state_path(tmp_path, X).exists()
    assert page_calls(fake) == [X]
    assert (report["removed"], report["pages_removed"]) == ([D], 1)


def test_folder_of_listing_miss_survives(tmp_path):
    fake = two_roots()
    run(fake, tmp_path)
    p_file, p_state = (tmp_path / R2 / f"{P}.md").read_bytes(), state_path(tmp_path, P).read_bytes()
    fake.listing["page"] = [R1]
    del fake.pages[R2]
    fake.calls.clear()
    report = run(fake, tmp_path, now=LATER)
    assert (tmp_path / R2 / f"{P}.md").read_bytes() == p_file
    assert state_path(tmp_path, P).read_bytes() == p_state
    assert md_files(tmp_path) == [f"{R1}/{R1}.md", f"{R2}/{P}.md"]
    assert not state_path(tmp_path, R2).exists()
    assert sorted(page_calls(fake)) == sorted([R2, P])
    assert roots_json(tmp_path) == {R1: "R1 title", R2: "R2 title"}
    assert report == {"roots": 2, "pages": 2, "sections": 2, "read": 0, "removed": [], "pages_removed": 1,
                      "unreadable": {}, "root_titles": {R1: "R1 title", R2: "R2 title"}}


def test_zero_kept_skips_removal(tmp_path):
    fake = two_roots()
    run(fake, tmp_path)
    before = tree(tmp_path)
    fake.listing = {"page": [], "database": []}
    fake.calls.clear()
    report = run(fake, tmp_path, now=LATER)
    assert tree(tmp_path) == before
    assert {method for method, _ in fake.calls} == {"search"}
    assert report == {"roots": 0, "pages": 0, "sections": 0, "read": 0, "removed": [], "pages_removed": 0,
                      "unreadable": {}, "root_titles": {}, "removal_skipped": True}


def test_every_page_unreadable_skips_removal(tmp_path):
    fake = FakeNotion()
    add_page(fake, R, "R title")
    fake.blocks[B] = block("column", B, {}, True, parent=under("page_id", R))
    add_page(fake, P, "P title", under("block_id", B))
    add_page(fake, X, "X title")
    run(fake, tmp_path)
    before = tree(tmp_path)
    del fake.pages[R], fake.pages[X]
    fake.fail[("block", B)] = NotionError(500, "server_error")
    fake.calls.clear()
    report = run(fake, tmp_path, now=LATER)
    assert tree(tmp_path) == before
    assert page_calls(fake) == []
    assert report == {"roots": 1, "pages": 1, "sections": 1, "read": 0, "removed": [], "pages_removed": 0,
                      "unreadable": {P: "server_error"}, "root_titles": {R: "R title"}, "removal_skipped": True}


def test_only_id_shaped_names_deleted(tmp_path):
    fake = two_roots()
    run(fake, tmp_path)
    foreign = {"notes/keep.md": "mine", f"notes/{P}.md": "mine", ".claude/CLAUDE.md": "mine",
               f"{R2}/readme.md": "mine", f"{R2}/{P}.md.tmp": "partial", f"{R2}/.{P}.md.x1.tmp": "partial",
               f"{R2[:-1]}/{P}.md": "mine", f"{R2}0/{P}.md": "mine", ".state/notes.json": "{}",
               ".state/pages/notes.json": "{}", f".state/pages/{P}.json.tmp": "{}"}
    for name, text in foreign.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    del fake.pages[R2], fake.pages[P]
    report = run(fake, tmp_path, now=LATER)
    assert (report["removed"], report["pages_removed"]) == ([R2], 2)
    assert not (tmp_path / R2 / f"{R2}.md").exists()
    assert not (tmp_path / R2 / f"{P}.md").exists()
    assert not state_path(tmp_path, P).exists()
    for name, text in foreign.items():
        assert (tmp_path / name).read_text() == text
    before = tree(tmp_path)
    assert run(fake, tmp_path, now=LATER)["removed"] == []
    assert tree(tmp_path) == before


def test_symlinked_folder_not_followed(tmp_path):
    out, elsewhere = tmp_path / "out", tmp_path / "elsewhere"
    run(root_and_child(), out)
    elsewhere.mkdir()
    (elsewhere / f"{Q}.md").write_text("not ours")
    (out / X).symlink_to(elsewhere, target_is_directory=True)
    fake = root_and_child()
    report = run(fake, out, now=LATER)
    assert (elsewhere / f"{Q}.md").read_text() == "not ours"
    assert (out / X).is_symlink()
    assert page_calls(fake) == []
    assert (report["removed"], report["pages_removed"]) == ([], 0)


def test_unreadable_page_not_deleted(tmp_path):
    fake = FakeNotion()
    add_page(fake, R, "R title")
    fake.blocks[B] = block("column", B, {}, True, parent=under("page_id", R))
    add_page(fake, P, "P title", under("block_id", B))
    add_page(fake, X, "X title")
    run(fake, tmp_path)
    p_file, p_state = (tmp_path / R / f"{P}.md").read_bytes(), state_path(tmp_path, P).read_bytes()
    del fake.pages[R]
    fake.fail[("block", B)] = NotionError(500, "server_error")
    report = run(fake, tmp_path, now=LATER)
    assert (tmp_path / R / f"{P}.md").read_bytes() == p_file
    assert state_path(tmp_path, P).read_bytes() == p_state
    assert md_files(tmp_path) == [f"{R}/{P}.md", f"{X}/{X}.md"]
    assert report["unreadable"] == {P: "server_error"}
    assert (report["removed"], report["pages_removed"]) == ([], 1)
    assert report["root_titles"] == {R: "R title", X: "X title"}


def test_page_checked_once_per_run(tmp_path):
    fake = root_and_child()
    run(fake, tmp_path)
    del fake.pages[R]
    fake.calls.clear()
    report = run(fake, tmp_path, now=LATER)
    assert page_calls(fake) == [R]
    assert md_files(tmp_path) == [f"{P}/{P}.md"]
    assert not state_path(tmp_path, R).exists()
    assert not (tmp_path / R).exists()
    assert (report["removed"], report["pages_removed"], report["read"]) == ([R], 1, 1)
