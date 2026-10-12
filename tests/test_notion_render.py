from mdexport.notion_render import FORMAT, compact_id, database_title, page_title, render_page, rich_text

URL = "https://www.notion.so/Plan-1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"
HEADING_ID = "9f8e7d6c-5b4a-3928-1706-f5e4d3c2b1a0"
PAGE = {
    "id": "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d",
    "url": URL,
    "last_edited_time": "2026-10-08T09:12:00.000Z",
    "parent": {"type": "workspace"},
    "properties": {"title": {"type": "title", "title": [{"plain_text": "Plan"}]}},
}


def _rt(text: str, href: str | None = None) -> list[dict]:
    return [{"plain_text": text, "href": href}]


def _block(kind: str, data: dict, children: list[dict] | None = None, block_id: str = "b0") -> dict:
    block = {"type": kind, "id": block_id, kind: data}
    if children is not None:
        block["_children"] = children
    return block


def _text(kind: str, text: str, children: list[dict] | None = None, block_id: str = "b0", **extra) -> dict:
    return _block(kind, {"rich_text": _rt(text), **extra}, children, block_id)


def _para(text: str, children: list[dict] | None = None) -> dict:
    return _text("paragraph", text, children)


def _heading(text: str, block_id: str = HEADING_ID, kind: str = "heading_2", **extra) -> dict:
    return _text(kind, text, block_id=block_id, is_toggleable=False, **extra)


def _render(blocks: list[dict], page: dict = PAGE, titles: dict | None = None) -> str:
    return render_page(page, blocks, ["Plan"], "Plan", titles or {})[0]


def _page_item(blocks: list[dict], page: dict = PAGE, titles: dict | None = None) -> list[str]:
    """The page item's lines after its link."""
    return _render(blocks, page, titles).rstrip("\n").split("\n\n")[1].split("\n")[2:]


def test_format_is_one():
    assert FORMAT == 1


def test_page_item_then_heading_items():
    blocks = [_para("The plan covers six weeks."), _heading("Hero Test"), _para("It starts Monday.")]
    text, sections = render_page(PAGE, blocks, ["Clients", "Plan"], "Clients", {})
    assert text == (
        "# Plan\n"
        "root: Clients · path: Clients › Plan · edited 2026-10-08\n"
        "\n"
        "## Plan\n"
        "https://www.notion.so/Plan-1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d\n"
        "The plan covers six weeks.\n"
        "\n"
        "## Plan › Hero Test\n"
        "https://www.notion.so/Plan-1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d#9f8e7d6c5b4a39281706f5e4d3c2b1a0\n"
        "It starts Monday.\n"
    )
    assert sections == 2


def test_text_before_first_heading_in_page_item():
    text = _render([_para("first"), _para("second"), _heading("Next"), _para("third")])
    assert text.endswith(
        "## Plan\n"
        f"{URL}\n"
        "first\n"
        "second\n"
        "\n"
        "## Plan › Next\n"
        f"{URL}#9f8e7d6c5b4a39281706f5e4d3c2b1a0\n"
        "third\n"
    )


def test_heading_with_newline_stays_one_line():
    text, sections = render_page(PAGE, [_heading("Hero\nTest\r2"), _para("body")], ["Plan"], "Plan", {})
    lines = text.splitlines()
    at = lines.index("## Plan › Hero Test 2")
    assert lines[at + 1 : at + 3] == [f"{URL}#9f8e7d6c5b4a39281706f5e4d3c2b1a0", "body"]
    assert sections == 2


def test_title_with_hash_and_newline():
    page = {**PAGE, "properties": {"Name": {"type": "title", "title": [{"plain_text": "#1 plan\nv2"}]}}}
    text = _render([_heading("Next")], page)
    assert text.startswith("# #1 plan v2\nroot: Plan · path: Plan · edited 2026-10-08\n\n## #1 plan v2\n")
    assert "## #1 plan v2 › Next" in text.splitlines()


def test_escaping():
    blocks = [_para("# not a heading"), _para("  ```js"), _para("~~~"), _para("a\r# b")]
    assert _page_item(blocks) == ["\\# not a heading", "  \\```js", "\\~~~", "a", "\\# b"]


def test_code_without_fence():
    code = _text("code", "x = 1\n```", language="python")
    assert _page_item([code]) == ["Code (python):", "    x = 1", "    \\```"]
    assert not any(line.lstrip().startswith("```") for line in _render([code]).splitlines())


def test_nested_lists_and_toggles():
    blocks = [
        _text("bulleted_list_item", "a", [_text("bulleted_list_item", "b")]),
        _text("toggle", "t", [_para("inner")]),
    ]
    assert _page_item(blocks) == ["- a", "  - b", "▸ t", "  inner"]


def test_other_markers():
    blocks = [
        _text("numbered_list_item", "one", [_text("to_do", "sub", checked=True)]),
        _text("to_do", "open", checked=False),
        _text("quote", "said", [_para("more")]),
        _text("callout", "note", icon={"type": "emoji", "emoji": "💡"}),
        _text("callout", "plain", icon={"type": "external", "external": {"url": "https://x/i.png"}}),
        _block("equation", {"expression": "e=mc^2"}),
    ]
    assert _page_item(blocks) == [
        "1. one", "  - [x] sub", "- [ ] open", "> said", "more", "> 💡 note", "> plain", "$$e=mc^2$$",
    ]


def test_multiline_text_continues_two_spaces_deeper():
    blocks = [_text("bulleted_list_item", "first\n# second", [_text("bulleted_list_item", "x\ny")])]
    assert _page_item(blocks) == ["- first", "  \\# second", "  - x", "    y"]


def test_blank_lines_inside_text_dropped():
    blocks = [_para("a\n\nb"), _text("bulleted_list_item", "\nx\n  \n\ny"), _para("\n\n")]
    assert _render(blocks) == (
        "# Plan\n"
        "root: Plan · path: Plan · edited 2026-10-08\n"
        "\n"
        "## Plan\n"
        f"{URL}\n"
        "a\n"
        "b\n"
        "- x\n"
        "  y\n"
    )


def test_trailing_blank_lines_end_file_with_one_newline():
    text = _render([_heading("H"), _para("c\n\n")])
    assert text == (
        "# Plan\n"
        "root: Plan · path: Plan · edited 2026-10-08\n"
        "\n"
        "## Plan\n"
        f"{URL}\n"
        "\n"
        "## Plan › H\n"
        f"{URL}#9f8e7d6c5b4a39281706f5e4d3c2b1a0\n"
        "c\n"
    )


def test_paragraph_children_one_level_deeper():
    assert _page_item([_para("p", [_para("child", [_para("grandchild")])])]) == ["p", "  child", "    grandchild"]


def test_rich_text_links():
    assert rich_text([{"plain_text": "docs", "href": "https://x.io"}, {"plain_text": " now", "href": None}]) == (
        "[docs](https://x.io) now"
    )


def test_columns_and_synced_flatten():
    blocks = [
        _block("column_list", {}, [
            _block("column", {}, [_para("left")]),
            _block("column", {}, [_para("right"), _heading("Side"), _para("in column")]),
        ]),
        _block("synced_block", {"synced_from": None}, [_para("synced")]),
    ]
    text, sections = render_page(PAGE, blocks, ["Plan"], "Plan", {})
    assert text.endswith(
        "## Plan\n"
        f"{URL}\n"
        "left\n"
        "right\n"
        "\n"
        "## Plan › Side\n"
        f"{URL}#9f8e7d6c5b4a39281706f5e4d3c2b1a0\n"
        "in column\n"
        "synced\n"
    )
    assert sections == 2


def test_toggleable_heading_children():
    heading = _text("heading_3", "Details", [_para("hidden")], block_id=HEADING_ID, is_toggleable=True)
    assert _render([heading, _para("after")]).endswith(
        "## Plan › Details\n"
        f"{URL}#9f8e7d6c5b4a39281706f5e4d3c2b1a0\n"
        "hidden\n"
        "after\n"
    )


def test_heading_with_empty_text_is_page_title():
    text = _render([_heading("")])
    assert text.endswith(f"## Plan\n{URL}#9f8e7d6c5b4a39281706f5e4d3c2b1a0\n")


def test_heading_nested_in_toggle_is_text():
    text, sections = render_page(PAGE, [_text("toggle", "t", [_heading("# Inner")])], ["Plan"], "Plan", {})
    assert text.endswith(f"{URL}\n▸ t\n  \\# Inner\n")
    assert sections == 1


ROW = {
    **PAGE,
    "parent": {"type": "database_id", "database_id": "d0d0d0d0-0000-0000-0000-000000000000"},
    "properties": {
        "Name": {"type": "title", "title": [{"plain_text": "Plan"}]},
        "Status": {"type": "status", "status": {"name": "In progress"}},
        "Owner": {"type": "people", "people": [{"object": "user", "id": "u1", "name": "Jay"}]},
        "Due": {"type": "date", "date": {"start": "2026-10-12", "end": "2026-10-14"}},
        "Tags": {"type": "multi_select", "multi_select": [{"name": "a"}, {"name": "b"}]},
        "Done": {"type": "checkbox", "checkbox": False},
        "Client": {"type": "relation", "relation": [{"id": "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"}]},
        "Notes": {"type": "rich_text", "rich_text": []},
        "Rollup": {"type": "rollup", "rollup": {"type": "number", "number": 3}},
    },
}


def test_database_row_properties():
    titles = {"aaaaaaaabbbbccccddddeeeeeeeeeeee": "Seed"}
    assert _page_item([_para("body")], ROW, titles) == [
        "Client: Seed",
        "Done: no",
        "Due: 2026-10-12 → 2026-10-14",
        "Owner: Jay",
        "Status: In progress",
        "Tags: a, b",
        "body",
    ]


def test_more_property_types():
    row = {**ROW, "properties": {
        "Budget": {"type": "number", "number": 0},
        "Priority": {"type": "select", "select": {"name": "High"}},
        "Empty select": {"type": "select", "select": None},
        "Site": {"type": "url", "url": "https://seed.com"},
        "Mail": {"type": "email", "email": "jay@seed.com"},
        "Phone": {"type": "phone_number", "phone_number": "+1 555"},
        "Score": {"type": "formula", "formula": {"type": "number", "number": 7}},
        "Live": {"type": "formula", "formula": {"type": "boolean", "boolean": True}},
        "Label": {"type": "formula", "formula": {"type": "string", "string": "ok"}},
        "Start": {"type": "date", "date": {"start": "2026-10-12", "end": None}},
        "Brief": {"type": "files", "files": [{"name": "brief.pdf", "type": "file", "file": {"url": "https://s3/x"}}]},
        "Created": {"type": "created_time", "created_time": "2026-09-01T10:00:00.000Z"},
        "Edited": {"type": "last_edited_time", "last_edited_time": "2026-10-08T09:12:00.000Z"},
        "Elsewhere": {"type": "relation", "relation": [{"id": "ffffffffffffffffffffffffffffffff"}]},
        "Nobody": {"type": "people", "people": [{"object": "user", "id": "u2"}]},
        "Unknown": {"type": "unique_id", "unique_id": {"prefix": "T", "number": 4}},
        "# Odd": {"type": "rich_text", "rich_text": _rt("[link](https://x)")},
    }}
    assert _page_item([], row) == [
        "\\# Odd: [link](https://x)",
        "Brief: brief.pdf",
        "Budget: 0",
        "Created: 2026-09-01",
        "Edited: 2026-10-08",
        "Label: ok",
        "Live: yes",
        "Mail: jay@seed.com",
        "Phone: +1 555",
        "Priority: High",
        "Score: 7",
        "Site: https://seed.com",
        "Start: 2026-10-12",
    ]


def test_property_values_are_one_line():
    row = {**ROW, "properties": {
        "Notes": {"type": "rich_text", "rich_text": _rt("first\nsecond")},
        "Label": {"type": "formula", "formula": {"type": "string", "string": "x\r\n\n# y"}},
        "Blank": {"type": "rich_text", "rich_text": _rt("  \n ")},
    }}
    assert _page_item([], row) == ["Label: x # y", "Notes: first second"]


def test_property_names_and_link_urls_are_one_line():
    row = {**ROW, "properties": {"Next\n# step": {"type": "rich_text", "rich_text": _rt("call")}}}
    assert _page_item([], row) == ["Next # step: call"]
    blocks = [
        _block("image", {"type": "external", "external": {"url": "https://x/a\n# b.png"}, "caption": _rt("hero")}),
        _block("bookmark", {"caption": [], "url": "https://x/c\n# d"}),
    ]
    assert _page_item(blocks) == ["File: hero (https://x/a # b.png)", "Link: [https://x/c # d](https://x/c # d)"]


def test_properties_only_for_database_rows():
    page = {**ROW, "parent": {"type": "page_id", "page_id": "p"}}
    assert _page_item([_para("body")], page) == ["body"]


def test_table_rows():
    rows = [
        _block("table_row", {"cells": [_rt("a"), _rt("b\nc")]}),
        _block("table_row", {"cells": [_rt("# 1"), []]}),
    ]
    blocks = [_block("table", {"table_width": 2}, rows)]
    assert _page_item(blocks) == ["| a | b c |", "| # 1 |  |"]


def test_media_links():
    blocks = [
        _block("image", {"type": "file", "file": {"url": "https://s3/hero.png?sig=1"}, "caption": _rt("hero")}),
        _block("image", {"type": "external", "external": {"url": "https://x/y.png"}, "caption": []}),
        _block("file", {"type": "file", "file": {"url": "https://s3/a/upload-7?sig=2"}, "caption": [],
                        "name": "brief.pdf"}),
        _block("pdf", {"type": "file", "file": {"url": "https://s3/a/My%20Deck.pdf?sig=3"}, "caption": []}),
        _block("video", {"type": "file", "file": {"url": ""}, "caption": []}),
    ]
    assert _page_item(blocks) == [
        "File: hero",
        "File: y.png (https://x/y.png)",
        "File: brief.pdf",
        "File: My Deck.pdf",
        "File: video",
    ]


def test_links_and_pages():
    blocks = [
        _block("bookmark", {"caption": _rt("Seed site"), "url": "https://seed.com"}),
        _block("embed", {"caption": [], "url": "https://figma.com/f"}),
        _block("link_preview", {"url": "https://github.com/x/pull/1"}),
        _block("child_page", {"title": "Sub plan"}),
        _block("child_database", {"title": "Tasks"}),
        _block("link_to_page", {"type": "page_id", "page_id": "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"}),
        _block("link_to_page", {"type": "database_id", "database_id": "ffffffff-ffff-ffff-ffff-ffffffffffff"}),
    ]
    assert _page_item(blocks, titles={"aaaaaaaabbbbccccddddeeeeeeeeeeee": "Seed"}) == [
        "Link: [Seed site](https://seed.com)",
        "Link: [https://figma.com/f](https://figma.com/f)",
        "Link: [https://github.com/x/pull/1](https://github.com/x/pull/1)",
        "Sub-page: Sub plan",
        "Database: Tasks",
        "Link to page: Seed",
        "Link to page: ffffffffffffffffffffffffffffffff",
    ]


def test_left_out_blocks():
    blocks = [
        _block("divider", {}),
        _block("table_of_contents", {"color": "default"}),
        _block("breadcrumb", {}),
        _block("unsupported", {}),
        _block("template", {"rich_text": _rt("tpl")}),
        _block("ai_block", {"rich_text": _rt("new")}),
    ]
    text, sections = render_page(PAGE, blocks, ["Plan"], "Plan", {})
    assert text == f"# Plan\nroot: Plan · path: Plan · edited 2026-10-08\n\n## Plan\n{URL}\n"
    assert sections == 1


def test_untitled():
    page = {**PAGE, "properties": {"title": {"type": "title", "title": []}}}
    assert _render([], page).startswith("# Untitled\n")
    assert page_title({**PAGE, "properties": {}}) == "Untitled"
    assert database_title({"title": []}) == "Untitled"
    assert database_title({"title": [{"plain_text": "Clients\nCRM", "href": None}]}) == "Clients CRM"


def test_compact_id():
    assert compact_id("1A2B3C4D-5E6F-7A8B-9C0D-1E2F3A4B5C6D") == "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"
