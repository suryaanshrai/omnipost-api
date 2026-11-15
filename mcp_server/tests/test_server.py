import json

import anyio
import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from omnipost_mcp.client import OmniPostClient
from omnipost_mcp.server import build_server


class FakeApi:
    """Records requests and answers like the OmniPost API."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, request.url.path, body))
        assert request.headers["Authorization"] == "Bearer op_test"
        path = request.url.path
        if path == "/workspaces/":
            return httpx.Response(200, json={"results": [{"id": 7, "name": "Me", "timezone": "UTC"}]})
        if path == "/channels/":
            assert request.url.params["workspace"] == "7"
            return httpx.Response(
                200, json={"results": [{"id": 3, "display_name": "@me", "connector_slug": "bluesky", "health": "ok"}]}
            )
        if path == "/posts/" and request.method == "POST":
            return httpx.Response(
                201, json={"id": 11, "status": "draft", "base_text": body["base_text"], "targets": []}
            )
        if path == "/posts/11/schedule/":
            return httpx.Response(
                200, json={"id": 11, "status": "scheduled", "targets": [{"channel": 3, "status": "scheduled"}]}
            )
        if path == "/posts/12/schedule/":
            return httpx.Response(400, json={"detail": "Cannot schedule a post in status 'published'."})
        if path == "/validate/":
            return httpx.Response(
                200, json={"channel": body["channel"], "ok": False, "findings": [{"code": "too_long"}]}
            )
        return httpx.Response(404, json={"detail": "Not found."})


def _server(api, **kwargs):
    return build_server(OmniPostClient("http://api.test", "op_test", transport=httpx.MockTransport(api)), **kwargs)


def _call(server, name, **arguments):
    async def run():
        return await server.call_tool(name, arguments)

    return anyio.run(run)


def _data(result):
    if getattr(result, "structured_content", None) is not None:
        data = result.structured_content
        return data.get("result", data)
    return json.loads(result.content[0].text)


def test_lists_channels_in_the_default_workspace():
    api = FakeApi()
    result = _data(_call(_server(api), "list_channels"))
    assert result == [{"id": 3, "name": "@me", "network": "bluesky", "health": "ok"}]


def test_create_draft_then_publish():
    api = FakeApi()
    server = _server(api)
    draft = _data(_call(server, "create_draft", text="Hello", channel_ids=[3], first_comment="https://ex.com"))
    assert draft["id"] == 11 and draft["status"] == "draft"
    created = [c for c in api.calls if c[:2] == ("POST", "/posts/")][0][2]
    assert created["workspace"] == 7
    assert created["target_specs"] == [{"channel": 3, "first_comment": "https://ex.com"}]

    published = _data(_call(server, "publish_now", post_id=11))
    assert published["status"] == "scheduled"
    assert "run_at" in api.calls[-1][2]


def test_api_errors_come_back_as_tool_errors():
    # The protocol layer turns this into an is_error result with the message.
    with pytest.raises(ToolError, match="Cannot schedule a post in status 'published'"):
        _call(_server(FakeApi()), "publish_now", post_id=12)


def test_check_post_reports_each_channels_problems():
    api = FakeApi()
    problems = _data(_call(_server(api), "check_post", text="x" * 400, channel_ids=[3, 4]))
    assert [p["channel"] for p in problems] == [3, 4]


def test_drafts_only_mode_has_no_publishing_tools():
    async def names(server):
        return {t.name for t in await server.list_tools()}

    assert "publish_now" in anyio.run(names, _server(FakeApi()))
    drafts_only = anyio.run(names, _server(FakeApi(), drafts_only=True))
    assert "create_draft" in drafts_only and not {"publish_now", "schedule_post", "add_to_queue"} & drafts_only


@pytest.fixture(autouse=True)
def _no_default_workspace_env(monkeypatch):
    monkeypatch.delenv("OMNIPOST_WORKSPACE", raising=False)
