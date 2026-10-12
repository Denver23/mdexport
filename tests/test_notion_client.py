import itertools
import json

import httpx
import pytest

from mdexport.notion_client import NotionClient, NotionError


class Script:
    """A MockTransport handler answering with `replies` in turn (the last one repeats) and recording each request."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def ticking():
    """A clock that advances one second per call, so the throttle never sleeps."""
    seconds = itertools.count()
    return lambda: float(next(seconds))


def make(script: Script, clock=None) -> tuple[NotionClient, list[float]]:
    slept: list[float] = []
    client = NotionClient("ntn_test", transport=httpx.MockTransport(script), sleep=slept.append,
                          clock=clock or ticking())
    return client, slept


def ok(body: dict) -> httpx.Response:
    return httpx.Response(200, json=body)


def body(request: httpx.Request) -> dict:
    return json.loads(request.content)


def test_search_follows_cursors():
    a, b, c = {"id": "a"}, {"id": "b"}, {"id": "c"}
    script = Script(ok({"results": [a, b], "has_more": True, "next_cursor": "c2"}),
                    ok({"results": [c], "has_more": False, "next_cursor": None}))
    client, _ = make(script)
    assert client.search("page") == [a, b, c]
    first, second = (body(r) for r in script.requests)
    assert [r.method for r in script.requests] == ["POST", "POST"]
    assert {r.url.path for r in script.requests} == {"/v1/search"}
    for sent in (first, second):
        assert sent["filter"] == {"property": "object", "value": "page"}
        assert sent["page_size"] == 100
    assert "start_cursor" not in first
    assert second["start_cursor"] == "c2"


def test_children_follows_cursors():
    script = Script(ok({"results": [{"id": "b1"}, {"id": "b2"}], "has_more": True, "next_cursor": "k2"}),
                    ok({"results": [{"id": "b3"}], "has_more": False, "next_cursor": None}))
    client, _ = make(script)
    assert client.children("X") == [{"id": "b1"}, {"id": "b2"}, {"id": "b3"}]
    first, second = script.requests
    assert [r.method for r in script.requests] == ["GET", "GET"]
    assert {r.url.path for r in script.requests} == {"/v1/blocks/X/children"}
    assert dict(first.url.params) == {"page_size": "100"}
    assert dict(second.url.params) == {"page_size": "100", "start_cursor": "k2"}


def test_headers():
    script = Script(ok({"object": "page"}), ok({"object": "database"}), ok({"object": "block"}))
    client, _ = make(script)
    assert client.page("P") == {"object": "page"}
    assert client.database("D") == {"object": "database"}
    assert client.block("B") == {"object": "block"}
    assert [(r.method, r.url.path) for r in script.requests] == [
        ("GET", "/v1/pages/P"), ("GET", "/v1/databases/D"), ("GET", "/v1/blocks/B")]
    for request in script.requests:
        assert request.url.host == "api.notion.com"
        assert request.headers["Authorization"] == "Bearer ntn_test"
        assert request.headers["Notion-Version"] == "2022-06-28"
        assert request.headers["Content-Type"] == "application/json"
        assert request.extensions["timeout"] == {"connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0}


def test_retry_after_honoured():
    script = Script(httpx.Response(429, headers={"Retry-After": "2.5"}, json={"code": "rate_limited"}),
                    ok({"object": "page"}))
    client, slept = make(script)
    assert client.page("P") == {"object": "page"}
    assert slept == [2.5]
    assert len(script.requests) == 2


def test_server_error_backs_off():
    script = Script(httpx.Response(502), httpx.Response(502), ok({"object": "page"}))
    client, slept = make(script)
    assert client.page("P") == {"object": "page"}
    assert slept == [1, 2]


def test_gives_up_after_five_retries():
    # The first Retry-After is unparsable, the rest are missing: both sleep 1 s.
    script = Script(httpx.Response(429, headers={"Retry-After": "soon"}), httpx.Response(429))
    client, slept = make(script)
    with pytest.raises(NotionError) as caught:
        client.page("P")
    assert caught.value.status == 429
    assert caught.value.code == "rate_limited"
    assert len(script.requests) == 6
    assert slept == [1, 1, 1, 1, 1]


def test_network_error_retried_then_raised():
    script = Script(httpx.ConnectError("connection refused"))
    client, slept = make(script)
    with pytest.raises(NotionError) as caught:
        client.page("P")
    assert caught.value.status == 0
    assert caught.value.code == "network_error"
    assert len(script.requests) == 6
    assert slept == [1, 2, 4, 8, 16]


def test_client_error_not_retried():
    script = Script(httpx.Response(404, json={"object": "error", "code": "object_not_found"}))
    client, slept = make(script)
    with pytest.raises(NotionError) as caught:
        client.page("P")
    assert caught.value.status == 404
    assert caught.value.code == "object_not_found"
    assert len(script.requests) == 1
    assert slept == []


def test_throttle_spaces_calls():
    script = Script(ok({"object": "page"}))
    client, slept = make(script, clock=lambda: 100.0)
    client.page("P")
    assert slept == []
    client.page("P")
    assert slept == [0.34]


def test_token_not_in_error():
    script = Script(httpx.Response(401, json={"object": "error", "code": "unauthorized"}))
    client, _ = make(script)
    with pytest.raises(NotionError) as caught:
        client.search("page")
    err = caught.value
    assert err.code == "unauthorized"
    assert err.status == 401
    assert str(err) == "unauthorized"
    assert "ntn_test" not in str(err)
    assert "ntn_test" not in repr(err)


@pytest.mark.parametrize("content", [b"<html>busy</html>", b"", b"[1, 2]", b'"page"'])
def test_invalid_json_not_retried(content):
    script = Script(httpx.Response(200, content=content))
    client, slept = make(script)
    with pytest.raises(NotionError) as caught:
        client.page("P")
    err = caught.value
    assert err.status == 200
    assert err.code == "invalid_json"
    assert len(script.requests) == 1
    assert slept == []
    assert "ntn_test" not in repr(err)
    assert err.__context__ is None and err.__cause__ is None


@pytest.mark.parametrize("error", [httpx.TooManyRedirects("Exceeded maximum allowed redirects."),
                                   httpx.DecodingError("Malformed gzip body.")])
def test_other_http_error_not_retried(error):
    script = Script(error)
    client, slept = make(script)
    with pytest.raises(NotionError) as caught:
        client.page("P")
    err = caught.value
    assert err.status == 0
    assert err.code == "network_error"
    assert len(script.requests) == 1
    assert slept == []
    assert "ntn_test" not in str(err) and "ntn_test" not in repr(err)
    # The httpx error carries the request and its Authorization header, so it must not be chained.
    assert err.__context__ is None and err.__cause__ is None


def test_retry_after_not_finite_or_negative_sleeps_one():
    script = Script(*(httpx.Response(429, headers={"Retry-After": value}) for value in ("inf", "nan", "-3", "-inf")),
                    ok({"object": "page"}))
    client, slept = make(script)
    assert client.page("P") == {"object": "page"}
    assert slept == [1, 1, 1, 1]
    assert len(script.requests) == 5


def test_retry_after_capped_at_sixty():
    script = Script(httpx.Response(429, headers={"Retry-After": "3600"}),
                    httpx.Response(429, headers={"Retry-After": "60.5"}),
                    httpx.Response(429, headers={"Retry-After": "60"}),
                    ok({"object": "page"}))
    client, slept = make(script)
    assert client.page("P") == {"object": "page"}
    assert slept == [60, 60, 60]
