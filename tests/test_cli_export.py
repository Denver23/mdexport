import json
import time

import pytest
from click.testing import CliRunner
from slack_sdk import WebClient
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler, ServerErrorRetryHandler

from mdexport import notion_export, slack_export
from mdexport.cli import cli
from mdexport.notion_client import NotionError

from .fake_notion import FakeNotion
from .fake_slack import FakeClient

TOKEN = "xoxb-test"


def recent_msg(text: str, hours_ago: int = 1) -> dict:
    return {"type": "message", "ts": f"{int(time.time()) - hours_ago * 3600}.000001", "user": "U1", "text": text}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SLACK_TOKEN", TOKEN)
    fake, seen = FakeClient(), []
    monkeypatch.setattr(slack_export, "make_client", lambda token: seen.append(token) or fake)
    return fake, seen


def invoke(tmp_path, *extra: str):
    args = ["export", "slack", "--output", str(tmp_path / "out"), "--report", str(tmp_path / "report.json"), *extra]
    return CliRunner().invoke(cli, args)


def test_missing_token(tmp_path, env, monkeypatch):
    monkeypatch.delenv("SLACK_TOKEN")
    result = invoke(tmp_path)
    assert result.exit_code == 1
    assert "SLACK_TOKEN is not set" in result.output
    assert env[1] == []
    assert not (tmp_path / "report.json").exists()


@pytest.mark.parametrize(
    "days, message",
    [
        (("--history-days", "7", "--refetch-days", "14"), "--refetch-days must be at most --history-days"),
        (("--history-days", "0", "--refetch-days", "0"), "--history-days and --refetch-days must be at least 1"),
        (("--refetch-days", "0"), "--history-days and --refetch-days must be at least 1"),
    ],
)
def test_refetch_above_history(tmp_path, env, days, message):
    result = invoke(tmp_path, *days)
    assert result.exit_code == 1
    assert message in result.output
    assert env[1] == []
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "report.json").exists()


def test_success_writes_report(tmp_path, env):
    fake, seen = env
    fake.channels = [
        {"id": "C01", "name": "general", "is_member": True},
        {"id": "C09", "name": "client-acme", "is_member": True},
    ]
    fake.history = {"C01": [recent_msg("hello", 2), recent_msg("again", 1)]}
    fake.fail = {"C09": "ratelimited"}
    result = invoke(tmp_path)
    assert result.exit_code == 0, result.output
    assert result.output == "Exported 2 channels, 2 messages, 1 unreadable\n"
    report = json.loads((tmp_path / "report.json").read_text())
    assert report == {"channels": 2, "messages": 2, "removed": [], "unreadable": {"C09": "ratelimited"}}
    assert seen == [TOKEN]
    assert list((tmp_path / "out" / "C01").glob("*.md"))
    assert TOKEN not in result.output
    leaked = [p for p in tmp_path.rglob("*") if p.is_file() and TOKEN.encode() in p.read_bytes()]
    assert leaked == []
    assert not (tmp_path / ".mdexport").exists()
    assert not list(tmp_path.glob(".report.json.*"))


def test_options_reach_export(tmp_path, env):
    fake, _ = env
    fake.channels = [
        {"id": "C01", "name": "general", "is_member": True},
        {"id": "C02", "name": "random", "is_member": True},
        {"id": "C03", "name": "ops", "is_member": True},
    ]
    fake.history = {"C01": [recent_msg("kept"), {**recent_msg("skipped", 2), "user": "U2"}]}
    result = invoke(tmp_path, "--history-days", "30", "--refetch-days", "3",
                    "--exclude", "C02", "--exclude", "ops", "--exclude-user", "U2")
    assert result.exit_code == 0, result.output
    assert result.output == "Exported 1 channels, 1 messages, 0 unreadable\n"
    assert {kw["channel"] for m, kw in fake.calls if m == "conversations_history"} == {"C01"}
    oldest = float(fake.history_calls("C01")[0]["oldest"])
    assert 29 * 86400 < time.time() - oldest < 31 * 86400


def test_removal_skipped_is_echoed(tmp_path, env):
    fake, _ = env
    fake.channels = [{"id": "C01", "name": "general", "is_member": False}]
    result = invoke(tmp_path)
    assert result.exit_code == 0, result.output
    assert result.output == "Exported 0 channels, 0 messages, 0 unreadable; removal skipped: no channels kept\n"
    assert json.loads((tmp_path / "report.json").read_text())["removal_skipped"] is True


def test_listing_failure_exits_1(tmp_path, env):
    fake, _ = env
    fake.channels = [{"id": "C01", "name": "general", "is_member": True}]
    fake.list_fail_page = 1
    result = invoke(tmp_path)
    assert result.exit_code == 1
    assert "Slack channel listing failed" in result.output
    assert "Traceback" not in result.output
    assert not (tmp_path / "report.json").exists()


def test_make_client_retries_rate_limits_and_server_errors():
    client = slack_export.make_client(TOKEN)
    assert isinstance(client, WebClient)
    assert client.token == TOKEN
    retries = {type(h): h.max_retry_count for h in client.retry_handlers}
    assert retries[RateLimitErrorRetryHandler] == 5
    assert retries[ServerErrorRetryHandler] == 5


def test_help_lists_every_option():
    result = CliRunner().invoke(cli, ["export", "slack", "--help"])
    assert result.exit_code == 0
    for option in ("--output", "--history-days", "--refetch-days", "--exclude", "--exclude-user", "--report"):
        assert option in result.output
    assert "dedicated" in result.output
    assert "--token" not in result.output


NOTION_TOKEN = "ntn_test"
PAGE = "b0" * 16


@pytest.fixture
def notion_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NOTION_TOKEN", NOTION_TOKEN)
    fake, seen = FakeNotion(), []
    fake.pages[PAGE] = {
        "object": "page", "id": PAGE, "url": f"https://www.notion.so/{PAGE}",
        "last_edited_time": "2026-10-08T09:12:00.000Z", "parent": {"type": "workspace", "workspace": True},
        "archived": False, "in_trash": False,
        "properties": {"Name": {"type": "title", "title": [{"plain_text": "Plan", "href": None}]}},
    }
    fake.children[PAGE] = [{"object": "block", "id": "99" * 16, "type": "paragraph", "has_children": False,
                            "paragraph": {"rich_text": [{"plain_text": "hello", "href": None}]}}]
    monkeypatch.setattr(notion_export, "make_client", lambda token: seen.append(token) or fake)
    return fake, seen


def invoke_notion(tmp_path, *extra: str):
    args = ["export", "notion", "--output", str(tmp_path / "out"), "--report", str(tmp_path / "report.json"), *extra]
    return CliRunner().invoke(cli, args)


@pytest.mark.parametrize("unset", [True, False])
def test_notion_missing_token(tmp_path, notion_env, monkeypatch, unset):
    if unset:
        monkeypatch.delenv("NOTION_TOKEN")
    else:
        monkeypatch.setenv("NOTION_TOKEN", "")
    result = invoke_notion(tmp_path)
    assert result.exit_code == 1
    assert "NOTION_TOKEN is not set" in result.output
    assert notion_env[1] == []
    assert not (tmp_path / "report.json").exists()


def test_notion_bad_exclude(tmp_path, notion_env):
    result = invoke_notion(tmp_path, "--exclude", PAGE, "--exclude", "not-an-id")
    assert result.exit_code == 1
    assert "--exclude must be a Notion page or database id: not-an-id" in result.output
    assert notion_env[1] == []
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "report.json").exists()


def test_notion_exclude_with_dashes_accepted(tmp_path, notion_env):
    result = invoke_notion(tmp_path, "--exclude", "1A2B3C4D-5E6F-7A8B-9C0D-1E2F3A4B5C6D")
    assert result.exit_code == 0, result.output
    assert result.output == "Exported 1 roots, 1 pages (1 read), 0 unreadable\n"


def test_notion_exclude_reaches_export(tmp_path, notion_env):
    dashed = f"{PAGE[:8]}-{PAGE[8:12]}-{PAGE[12:16]}-{PAGE[16:20]}-{PAGE[20:]}".upper()
    result = invoke_notion(tmp_path, "--exclude", dashed)
    assert result.exit_code == 0, result.output
    assert result.output == "Exported 0 roots, 0 pages (0 read), 0 unreadable; removal skipped: no roots kept\n"
    assert not (tmp_path / "out" / PAGE / f"{PAGE}.md").exists()


def test_notion_success_writes_report_and_summary(tmp_path, notion_env):
    fake, seen = notion_env
    result = invoke_notion(tmp_path)
    assert result.exit_code == 0, result.output
    assert result.output == "Exported 1 roots, 1 pages (1 read), 0 unreadable\n"
    report = json.loads((tmp_path / "report.json").read_text())
    assert set(report) >= {"roots", "pages", "sections", "read", "removed", "pages_removed", "unreadable", "root_titles"}
    assert report["root_titles"] == {PAGE: "Plan"}
    assert (tmp_path / "out" / PAGE / f"{PAGE}.md").is_file()
    assert seen == [NOTION_TOKEN]
    assert fake.closed
    assert NOTION_TOKEN not in result.output
    leaked = [p for p in tmp_path.rglob("*") if p.is_file() and NOTION_TOKEN.encode() in p.read_bytes()]
    assert leaked == []
    assert not (tmp_path / ".mdexport").exists()
    assert not list(tmp_path.glob(".report.json.*"))


def test_notion_listing_failure(tmp_path, notion_env):
    fake, _ = notion_env
    fake.list_fail["page"] = NotionError(401, "unauthorized")
    result = invoke_notion(tmp_path)
    assert result.exit_code == 1
    assert "Notion listing failed: unauthorized" in result.output
    assert "Traceback" not in result.output
    assert NOTION_TOKEN not in result.output
    assert fake.closed
    assert not (tmp_path / "report.json").exists()


def test_notion_removal_skipped_suffix(tmp_path, notion_env, monkeypatch):
    zeros = {"roots": 0, "pages": 0, "sections": 0, "read": 0, "removed": [], "pages_removed": 0,
             "unreadable": {}, "root_titles": {}, "removal_skipped": True}
    monkeypatch.setattr(notion_export, "export", lambda client, out, **kw: zeros)
    result = invoke_notion(tmp_path)
    assert result.exit_code == 0, result.output
    assert result.output == "Exported 0 roots, 0 pages (0 read), 0 unreadable; removal skipped: no roots kept\n"
    assert json.loads((tmp_path / "report.json").read_text())["removal_skipped"] is True


def test_notion_help():
    result = CliRunner().invoke(cli, ["export", "notion", "--help"])
    assert result.exit_code == 0
    for text in ("--output", "--exclude", "--report", "NOTION_TOKEN", "OUTPUT/.state/", "dedicated", "shared"):
        assert text in result.output
    assert "--token" not in result.output
