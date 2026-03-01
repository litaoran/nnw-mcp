# nnw-mcp

A read-only MCP server that gives Claude (or any MCP client) direct access to
your [NetNewsWire](https://netnewswire.com) RSS feeds via the app's local SQLite
databases — no network calls, no API keys.

## Available Tools

| Tool | Description |
|------|-------------|
| `list_accounts` | All NetNewsWire accounts with feed counts |
| `list_feeds` | Every subscribed feed (title, URL, folder) |
| `get_unread_articles` | Unread articles, optionally filtered by feed |
| `get_today_articles` | Articles that arrived since midnight |
| `get_starred_articles` | Your starred / bookmarked articles |
| `get_articles_by_feed` | All articles for a specific feed URL |
| `search_articles` | Full-text search across titles + body text |
| `get_article_content` | Full HTML/text content + authors for one article |

## Setup

### 1. Install dependencies

```bash
cd ~/projects/nnw-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install mcp[cli]
```

### 2. Test the server

```bash
mcp dev server.py
```

This opens the MCP Inspector where you can call each tool interactively.

### 3. Add to Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "netnewswire": {
      "command": "/Users/taoranli/projects/nnw-mcp/.venv/bin/python",
      "args": ["/Users/taoranli/projects/nnw-mcp/server.py"]
    }
  }
}
```

Restart Claude Desktop — you'll see the 🔌 MCP icon and the tools will be
available in every conversation.

## Notes

- **Read-only**: the server opens SQLite in `?mode=ro`, so it cannot modify
  your data or mark articles as read.
- **No app required**: works whether NetNewsWire is running or not.
- **Multi-account**: automatically discovers all accounts (On My Mac, Feedly,
  Feedbin, etc.) under the sandbox container.
- **Timestamps**: `dateArrived` and `datePublished` are Unix timestamps
  (seconds since epoch).
