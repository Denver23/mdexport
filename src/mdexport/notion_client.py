"""A small Notion REST client - throttled to 3 calls per second, retrying rate limits, server and network errors."""

import math
import time

import httpx

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
TIMEOUT = 30.0
MIN_INTERVAL = 0.34
MAX_RETRIES = 5
PAGE_SIZE = 100
RETRY_AFTER_DEFAULT = 1.0
RETRY_AFTER_MAX = 60.0


class NotionError(Exception):
    """A Notion call failed; `status` is 0 for a network error, and `str(e)` is the code, never the token."""

    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def _code(resp: httpx.Response, fallback: str) -> str:
    """Notion's error `code` from the response body, else `fallback`."""
    try:
        code = resp.json().get("code")
    except (ValueError, AttributeError):
        return fallback
    return code if isinstance(code, str) and code else fallback


def _retry_after(resp: httpx.Response) -> float:
    """Seconds from `Retry-After`, capped at 60; 1 when missing, unparsable, negative or not finite."""
    try:
        seconds = float(resp.headers["Retry-After"])
    except (KeyError, ValueError):
        return RETRY_AFTER_DEFAULT
    if not math.isfinite(seconds) or seconds < 0:
        return RETRY_AFTER_DEFAULT
    return min(seconds, RETRY_AFTER_MAX)


def _json_object(resp: httpx.Response) -> dict:
    """The body as a JSON object; any other body raises `NotionError(status, "invalid_json")`."""
    try:
        data = resp.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise NotionError(resp.status_code, "invalid_json")
    return data


class NotionClient:
    """Throttled, retrying Notion API calls; a failed call raises `NotionError`."""

    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None, sleep=time.sleep,
                 clock=time.monotonic):
        headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION,
                   "Content-Type": "application/json"}
        self._http = httpx.Client(base_url=API_BASE, headers=headers, timeout=TIMEOUT, transport=transport)
        self._sleep, self._clock = sleep, clock
        self._last: float | None = None

    def _throttle(self) -> None:
        """Sleep until `MIN_INTERVAL` has passed since the previous request."""
        if self._last is not None:
            wait = MIN_INTERVAL - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """One call's JSON object, after up to `MAX_RETRIES` retries of 429, 5xx and transport errors."""
        for retry in range(MAX_RETRIES + 1):
            self._throttle()
            try:
                resp = self._http.request(method, path, **kwargs)
            except httpx.TransportError:
                status, code, wait = 0, "network_error", 2**retry
            except httpx.HTTPError:
                break  # too many redirects or an undecodable body: a retry fails the same way
            else:
                status = resp.status_code
                if resp.is_success:
                    return _json_object(resp)
                if status == 429:
                    code, wait = _code(resp, "rate_limited"), _retry_after(resp)
                elif status >= 500:
                    code, wait = _code(resp, "server_error"), 2**retry
                else:
                    raise NotionError(status, _code(resp, f"http_{status}"))
            if retry == MAX_RETRIES:
                raise NotionError(status, code)
            self._sleep(wait)
        # Reached only by the break above; raising here, outside the except, chains no httpx error or its headers.
        raise NotionError(0, "network_error")

    def _all(self, method: str, path: str, arg: str, base: dict) -> list[dict]:
        """Every page of a paginated list; `arg` is `json` or `params`, where `start_cursor` goes."""
        results, cursor = [], None
        while True:
            payload = {**base, "start_cursor": cursor} if cursor else base
            data = self._request(method, path, **{arg: payload})
            results.extend(data.get("results") or [])
            cursor = data.get("next_cursor")
            if not (data.get("has_more") and cursor):
                return results

    def search(self, object_type: str) -> list[dict]:
        """Every page or database (`object_type`) shared with the integration."""
        query = {"filter": {"property": "object", "value": object_type}, "page_size": PAGE_SIZE}
        return self._all("POST", "/search", "json", query)

    def page(self, page_id: str) -> dict:
        """One page object."""
        return self._request("GET", f"/pages/{page_id}")

    def database(self, database_id: str) -> dict:
        """One database object."""
        return self._request("GET", f"/databases/{database_id}")

    def block(self, block_id: str) -> dict:
        """One block object."""
        return self._request("GET", f"/blocks/{block_id}")

    def children(self, block_id: str) -> list[dict]:
        """One level of a block's children, every page of them."""
        return self._all("GET", f"/blocks/{block_id}/children", "params", {"page_size": PAGE_SIZE})

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()


def make_client(token: str) -> NotionClient:
    """A Notion client; the one factory the CLI calls and tests patch."""
    return NotionClient(token)
