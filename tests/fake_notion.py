"""A stand-in for NotionClient that records calls, returns deep copies and raises configured errors."""

import copy

from mdexport.notion_client import NotionError


class _Children(dict):
    """The `children` table (block id -> list of blocks); calling it is `NotionClient.children`."""

    def __init__(self, fake: "FakeNotion", table: dict):
        super().__init__(table)
        self._fake = fake

    def __call__(self, block_id: str) -> list[dict]:
        return self._fake._get("children", self, block_id)


class FakeNotion:
    """Tables keyed by compact id; `listing[type]` lists ids (all when left out); `fail[(method, id)]` raises."""

    def __init__(self):
        self.pages: dict[str, dict] = {}
        self.databases: dict[str, dict] = {}
        self.blocks: dict[str, dict] = {}
        self.children = {}
        self.listing: dict[str, list[str]] = {}
        self.fail: dict[tuple[str, str], NotionError] = {}
        self.list_fail: dict[str, NotionError] = {}
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    # `children` is both the table and the NotionClient method: a dict that can be called.
    @property
    def children(self) -> _Children:
        return self._children

    @children.setter
    def children(self, table: dict) -> None:
        self._children = _Children(self, table)

    def _get(self, method: str, table: dict, item_id: str):
        self.calls.append((method, item_id))
        if (method, item_id) in self.fail:
            raise self.fail[(method, item_id)]
        if item_id not in table:
            raise NotionError(404, "object_not_found")
        return copy.deepcopy(table[item_id])

    def search(self, object_type: str) -> list[dict]:
        self.calls.append(("search", object_type))
        if object_type in self.list_fail:
            raise self.list_fail[object_type]
        table = self.pages if object_type == "page" else self.databases
        return [copy.deepcopy(table[item_id]) for item_id in self.listing.get(object_type, list(table))]

    def page(self, page_id: str) -> dict:
        return self._get("page", self.pages, page_id)

    def database(self, database_id: str) -> dict:
        return self._get("database", self.databases, database_id)

    def block(self, block_id: str) -> dict:
        return self._get("block", self.blocks, block_id)

    def close(self) -> None:
        self.closed = True
