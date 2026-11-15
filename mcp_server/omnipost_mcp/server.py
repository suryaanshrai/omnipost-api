"""OmniPost tools for MCP clients (Claude Desktop, Claude Code, others).

Run with OMNIPOST_API_URL and OMNIPOST_TOKEN set:

    omnipost-mcp                         # stdio, for desktop clients
    omnipost-mcp --http --port 8765      # streamable HTTP

OMNIPOST_WORKSPACE picks the default workspace (else your first one).
OMNIPOST_MCP_DRAFTS_ONLY=1 leaves out the tools that publish or schedule.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .client import OmniPostClient, OmniPostError

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
# Publishing reaches the outside world and can't be taken back.
PUBLISH = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)


def _post_summary(post: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": post["id"],
        "status": post["status"],
        "kind": post.get("kind"),
        "text": post.get("base_text", ""),
        "link": post.get("link"),
        "scheduled_for": post.get("scheduled_for"),
        "targets": [
            {
                "channel": t.get("channel"),
                "status": t.get("status"),
                "url": t.get("permalink"),
            }
            for t in post.get("targets", [])
        ],
    }


def build_server(client: OmniPostClient, drafts_only: bool = False) -> MCPServer:
    server = MCPServer(
        name="omnipost",
        instructions=(
            "OmniPost publishes one post to many social networks. Start with list_channels to get channel ids. "
            "create_draft saves without publishing. Only call publish_now, schedule_post or add_to_queue when the "
            "user has asked to post, and confirm the text and channels with them first."
        ),
    )
    default_workspace: dict[str, int] = {}

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except OmniPostError as exc:
            raise ToolError(str(exc)) from exc

    def workspace_id(given: int | None) -> int:
        if given:
            return given
        if "id" not in default_workspace:
            env = os.environ.get("OMNIPOST_WORKSPACE")
            if env:
                default_workspace["id"] = int(env)
            else:
                workspaces = call(client.results, "/workspaces/")
                if not workspaces:
                    raise ToolError("This account has no workspaces.")
                default_workspace["id"] = workspaces[0]["id"]
        return default_workspace["id"]

    @server.tool(annotations=READ)
    def list_workspaces() -> list[dict[str, Any]]:
        """Your workspaces (brands or clients). Most people have one."""
        return [
            {"id": w["id"], "name": w["name"], "timezone": w.get("timezone")}
            for w in call(client.results, "/workspaces/")
        ]

    @server.tool(annotations=READ)
    def list_channels(workspace_id_: int | None = None) -> list[dict[str, Any]]:
        """Connected social accounts you can post to, with their ids.
        `health` other than "ok" means the account needs reconnecting."""
        return [
            {"id": c["id"], "name": c["display_name"], "network": c["connector_slug"], "health": c.get("health")}
            for c in call(client.results, "/channels/", workspace=workspace_id(workspace_id_), limit=100)
        ]

    @server.tool(annotations=READ)
    def list_posts(
        status: str | None = None, search: str | None = None, limit: int = 20, workspace_id_: int | None = None
    ) -> list[dict[str, Any]]:
        """Recent posts, newest first. `status` is one or more of draft, in_review,
        approved, scheduled, publishing, published, partially_published, failed,
        canceled (comma-separated). `search` matches the post text."""
        posts = call(
            client.results,
            "/posts/",
            workspace=workspace_id(workspace_id_),
            status=status,
            q=search,
            limit=max(1, min(limit, 100)),
        )
        return [_post_summary(p) for p in posts]

    @server.tool(annotations=READ)
    def get_post(post_id: int) -> dict[str, Any]:
        """One post and where it went: each channel's status and link."""
        return _post_summary(call(client.get, f"/posts/{post_id}/"))

    @server.tool(annotations=READ)
    def post_performance(post_id: int) -> list[dict[str, Any]]:
        """Likes, comments, shares and impressions per channel, where the
        network reports them."""
        rows = call(client.get, f"/posts/{post_id}/performance/")
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        return rows

    @server.tool(annotations=WRITE)
    def create_draft(
        text: str,
        channel_ids: list[int],
        link: str | None = None,
        first_comment: str | None = None,
        workspace_id_: int | None = None,
    ) -> dict[str, Any]:
        """Saves a text post as a draft for the given channels. Nothing is
        published. `first_comment` is posted as a reply right after publishing
        (on networks that support it): a good place for links."""
        specs = [{"channel": cid, **({"first_comment": first_comment} if first_comment else {})} for cid in channel_ids]
        body: dict[str, Any] = {
            "workspace": workspace_id(workspace_id_),
            "kind": "text",
            "base_text": text,
            "target_specs": specs,
        }
        if link:
            body["link"] = link
        return _post_summary(call(client.post, "/posts/", body))

    @server.tool(annotations=READ)
    def check_post(text: str, channel_ids: list[int]) -> list[dict[str, Any]]:
        """Checks text against each channel's limits (length, links, media)
        before posting. Returns problems; an empty list means it fits."""
        problems = []
        for cid in channel_ids:
            result = call(client.post, "/validate/", {"channel": cid, "text": text, "post_kind": "text"})
            problems += [{"channel": cid, **f} for f in result.get("findings", [])]
        return problems

    if not drafts_only:

        @server.tool(annotations=PUBLISH)
        def publish_now(post_id: int) -> dict[str, Any]:
            """Publishes a draft to all its channels now. Can't be undone:
            confirm with the user first."""
            now = datetime.now(timezone.utc).isoformat()
            return _post_summary(call(client.post, f"/posts/{post_id}/schedule/", {"run_at": now}))

        @server.tool(annotations=PUBLISH)
        def schedule_post(post_id: int, run_at: str) -> dict[str, Any]:
            """Schedules a draft for `run_at` (ISO 8601 with a timezone, e.g.
            2030-01-07T09:00:00+01:00)."""
            return _post_summary(call(client.post, f"/posts/{post_id}/schedule/", {"run_at": run_at}))

        @server.tool(annotations=PUBLISH)
        def add_to_queue(post_id: int) -> dict[str, Any]:
            """Schedules a draft into each channel's next open queue slot."""
            return _post_summary(call(client.post, f"/posts/{post_id}/queue/"))

        @server.tool(annotations=WRITE)
        def cancel_post(post_id: int) -> dict[str, Any]:
            """Cancels a scheduled post on every channel it hasn't reached yet."""
            return _post_summary(call(client.post, f"/posts/{post_id}/cancel/"))

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="OmniPost MCP server")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = build_server(OmniPostClient.from_env(), drafts_only=os.environ.get("OMNIPOST_MCP_DRAFTS_ONLY") == "1")
    if args.http:
        server.run("streamable-http", host=args.host, port=args.port)
    else:
        server.run()


if __name__ == "__main__":
    main()
