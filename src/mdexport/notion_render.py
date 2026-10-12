"""Render one Notion page as a Markdown file of `##` passages, each with its link on the next line."""

from collections.abc import Iterator
from urllib.parse import unquote, urlsplit

from mdexport.slack_export import _escape_line

FORMAT = 1

_UNTITLED = "Untitled"
_HEADINGS = ("heading_1", "heading_2", "heading_3")
_FLATTENED = ("column_list", "column", "synced_block")
_MEDIA = ("image", "file", "pdf", "video", "audio")
_LINKS = ("bookmark", "embed", "link_preview")
_MARKERS = {"paragraph": "", "bulleted_list_item": "- ", "numbered_list_item": "1. ", "toggle": "▸ ", "quote": "> "}
# Extra indent levels for a block's children; blocks not listed render none.
_CHILD_INDENT = {"paragraph": 1, "bulleted_list_item": 1, "numbered_list_item": 1, "to_do": 1, "toggle": 1,
                 "quote": 0, "callout": 0}


def compact_id(notion_id: str) -> str:
    """`notion_id` as lowercase hex without dashes."""
    return notion_id.replace("-", "").lower()


def rich_text(items: list[dict]) -> str:
    """Notion rich text as plain text, with links as `[text](href)`."""
    return "".join(
        f"[{item.get('plain_text', '')}]({item['href']})" if item.get("href") else item.get("plain_text", "")
        for item in items
    )


def one_line(text: str) -> str:
    """`text` with every run of whitespace, line breaks included, as one space."""
    return " ".join(text.split())


def page_title(page: dict) -> str:
    """The page's title property as one line, or `Untitled`."""
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return one_line(rich_text(prop.get("title") or [])) or _UNTITLED
    return _UNTITLED


def database_title(database: dict) -> str:
    """The database's title as one line, or `Untitled`."""
    return one_line(rich_text(database.get("title") or [])) or _UNTITLED


def _property_value(prop: dict, titles: dict[str, str]) -> str:
    """A database property as text; empty when the property is left out."""
    kind = prop.get("type")
    value = prop.get(kind)
    if value is None:
        return ""
    if kind == "rich_text":
        return rich_text(value)
    if kind == "number":
        return str(value)
    if kind in ("select", "status"):
        return value.get("name", "")
    if kind == "multi_select":
        return ", ".join(option["name"] for option in value)
    if kind == "date":
        return f"{value['start']} → {value['end']}" if value.get("end") else value.get("start") or ""
    if kind == "people":
        return ", ".join(person["name"] for person in value if person.get("name"))
    if kind in ("checkbox", "boolean"):
        return "yes" if value else "no"
    if kind in ("url", "email", "phone_number", "string"):
        return value
    if kind == "formula":
        return _property_value(value, titles)
    if kind == "relation":
        related = (compact_id(item["id"]) for item in value)
        return ", ".join(titles[rid] for rid in related if rid in titles)
    if kind == "files":
        return ", ".join(f["name"] for f in value if f.get("name"))
    if kind in ("created_time", "last_edited_time"):
        return value[:10]
    return ""


def _text_lines(text: str, indent: int, marker: str = "") -> list[str]:
    """`marker` + `text` as lines at `indent`, blank ones dropped; after a marker, later lines sit two spaces deeper."""
    lines = [line for line in text.splitlines() if line.strip()]
    if marker:
        lines = [marker + (lines[0] if lines else ""), *lines[1:]]
    if not lines:
        return []
    pad = "  " * indent
    more = pad + "  " if marker else pad
    return [pad + lines[0]] + [more + line for line in lines[1:]]


def _property_lines(page: dict, titles: dict[str, str]) -> list[str]:
    if (page.get("parent") or {}).get("type") != "database_id":
        return []
    props = page.get("properties") or {}
    lines = []
    for name in sorted(props):
        value = one_line(_property_value(props[name], titles))
        if value:
            lines.append(f"{one_line(name)}: {value}")
    return lines


def _media_line(kind: str, data: dict) -> str:
    url = (data.get(data.get("type")) or {}).get("url") or ""
    segment = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    label = (one_line(rich_text(data.get("caption") or [])) or one_line(data.get("name") or "")
             or one_line(segment) or kind)
    return f"File: {label} ({one_line(url)})" if data.get("type") == "external" else f"File: {label}"


def _marker(kind: str, data: dict) -> str | None:
    """The marker a text block's first line carries; None for a block that is not a text block."""
    if kind == "to_do":
        return "- [x] " if data.get("checked") else "- [ ] "
    if kind == "callout":
        icon = data.get("icon") or {}
        return f"> {icon['emoji']} " if icon.get("type") == "emoji" else "> "
    return _MARKERS.get(kind)


def _block_lines(kind: str, data: dict, indent: int, titles: dict[str, str]) -> list[str]:
    """One block's own lines, without its children."""
    pad = "  " * indent
    marker = _marker(kind, data)
    if marker is not None:
        return _text_lines(rich_text(data.get("rich_text") or []), indent, marker)
    if kind == "code":
        code = rich_text(data.get("rich_text") or []).splitlines()
        return [f"{pad}Code ({data.get('language', '')}):"] + [f"{pad}    {line}" for line in code]
    if kind == "equation":
        return _text_lines(f"$${data.get('expression', '')}$$", indent)
    if kind in _MEDIA:
        return [pad + _media_line(kind, data)]
    if kind in _LINKS:
        url = one_line(data.get("url") or "")
        return [f"{pad}Link: [{one_line(rich_text(data.get('caption') or [])) or url}]({url})"]
    if kind == "child_page":
        return [f"{pad}Sub-page: {one_line(data.get('title') or '') or _UNTITLED}"]
    if kind == "child_database":
        return [f"{pad}Database: {one_line(data.get('title') or '') or _UNTITLED}"]
    if kind == "link_to_page":
        target = data.get(data.get("type"))
        if not isinstance(target, str):
            return []
        return [f"{pad}Link to page: {titles.get(compact_id(target), compact_id(target))}"]
    return []


def _walk(blocks: list[dict], indent: int, titles: dict[str, str], top: bool) -> Iterator[str | dict]:
    """Yield content lines, and the heading block itself where a heading starts a new item."""
    for block in blocks:
        kind = block.get("type")
        data = block.get(kind) or {}
        children = block.get("_children") or []
        if kind in _HEADINGS and top:
            yield block
            yield from _walk(children, 0, titles, False)
        elif kind in _HEADINGS:
            yield from _text_lines(rich_text(data.get("rich_text") or []), indent)
            yield from _walk(children, indent + 1, titles, False)
        elif kind in _FLATTENED:
            yield from _walk(children, indent, titles, top)
        elif kind == "table":
            for row in children:
                if row.get("type") == "table_row":
                    cells = (one_line(rich_text(cell)) for cell in row["table_row"].get("cells", []))
                    yield "  " * indent + "| " + " | ".join(cells) + " |"
        else:
            yield from _block_lines(kind, data, indent, titles)
            if kind in _CHILD_INDENT:
                yield from _walk(children, indent + _CHILD_INDENT[kind], titles, False)


def render_page(page: dict, blocks: list[dict], path: list[str], root_title: str,
                titles: dict[str, str]) -> tuple[str, int]:
    """Render one page's file text, a `##` item per heading after the page's own; return it and the item count."""
    title, url = page_title(page), page["url"]
    trail = " › ".join(one_line(part) for part in path)
    head = f"# {title}\nroot: {one_line(root_title)} · path: {trail} · edited {page['last_edited_time'][:10]}"
    # Only the heading and link lines are composed here; every content line is escaped.
    items = [[f"## {title}", url, *map(_escape_line, _property_lines(page, titles))]]
    for entry in _walk(blocks, 0, titles, True):
        if isinstance(entry, str):
            items[-1].append(_escape_line(entry))
            continue
        heading = one_line(rich_text(entry[entry["type"]].get("rich_text") or []))
        items.append([f"## {title} › {heading}" if heading else f"## {title}", f"{url}#{compact_id(entry['id'])}"])
    return "\n\n".join([head, *("\n".join(item) for item in items)]) + "\n", len(items)
