# omnipost-mcp

An [MCP](https://modelcontextprotocol.io) server for OmniPost. It lets Claude
(or any MCP client) draft, check, schedule and publish your posts, and read
how they did, through the OmniPost REST API.

## Set up

1. In OmniPost, open **Settings → API tokens** and make a token. Tick
   **Read-only** if the assistant should only look, not post.
2. Install: `pipx install ./mcp_server` (from the omnipost-api repo), or
   `pip install ./mcp_server` into any Python 3.10+ environment.
3. Add it to your client. Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "omnipost": {
      "command": "omnipost-mcp",
      "env": {
        "OMNIPOST_API_URL": "https://api.yourdomain.com",
        "OMNIPOST_TOKEN": "op_..."
      }
    }
  }
}
```

Claude Code: `claude mcp add omnipost -e OMNIPOST_API_URL=... -e OMNIPOST_TOKEN=op_... -- omnipost-mcp`

For clients that connect over HTTP, run `omnipost-mcp --http --port 8765`
and point the client at `http://127.0.0.1:8765/mcp`. It listens on
localhost only; anyone who can reach it posts as you.

## Settings

| Variable | |
|---|---|
| `OMNIPOST_API_URL` | Your API's address. Default `http://localhost:8000`. |
| `OMNIPOST_TOKEN` | A personal API token. Required. |
| `OMNIPOST_WORKSPACE` | Workspace id to use when a tool isn't given one. Default: your first. |
| `OMNIPOST_MCP_DRAFTS_ONLY` | `1` leaves out the tools that publish or schedule. |

## Tools

| Tool | Does |
|---|---|
| `list_workspaces`, `list_channels` | What you have and the ids to use |
| `list_posts`, `get_post` | Recent posts, filter by status or text; where each one went |
| `post_performance` | Likes, comments, shares, impressions per channel |
| `check_post` | Checks text against each channel's limits |
| `create_draft` | Saves a draft (optionally with a first comment). Publishes nothing |
| `publish_now`, `schedule_post`, `add_to_queue` | Sends a draft. Marked destructive so clients ask first |
| `cancel_post` | Stops a scheduled post |

## Develop

```bash
pip install -e ".[test]"
pytest
```
