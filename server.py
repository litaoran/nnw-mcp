#!/usr/bin/env python3
"""
NetNewsWire MCP Server

Provides read-only access to NetNewsWire RSS articles and feeds via MCP tools.
Reads directly from the SQLite databases in the app's sandbox container.
"""

import json
import sqlite3
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional

import feedparser
from mcp.server.fastmcp import FastMCP

# ── Database location ────────────────────────────────────────────────────────

NNW_ACCOUNTS = (
    Path.home()
    / "Library/Containers/com.ranchero.NetNewsWire-Evergreen"
    / "Data/Library/Application Support/NetNewsWire/Accounts"
)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _discover_accounts() -> list[dict]:
    """Return metadata for every account that has a DB.sqlite3."""
    accounts = []
    if not NNW_ACCOUNTS.exists():
        return accounts

    for account_dir in sorted(NNW_ACCOUNTS.iterdir()):
        if not account_dir.is_dir():
            continue
        db_path = account_dir / "DB.sqlite3"
        if not db_path.exists():
            continue

        # Try to get a human-readable name from the OPML header
        name = account_dir.name
        opml_path = account_dir / "Subscriptions.opml"
        if opml_path.exists():
            try:
                tree = ET.parse(opml_path)
                title_el = tree.find("./head/title")
                if title_el is not None and title_el.text:
                    name = title_el.text
            except ET.ParseError:
                pass

        accounts.append(
            {
                "name": name,
                "folder": account_dir.name,
                "db_path": db_path,
                "opml_path": opml_path if opml_path.exists() else None,
            }
        )
    return accounts


def _open_db(db_path: Path) -> sqlite3.Connection:
    """Open a read-only connection with Row factory."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_opml(opml_path: Path) -> list[dict]:
    """Return a flat list of feeds parsed from an OPML file."""
    feeds: list[dict] = []
    try:
        body = ET.parse(opml_path).find("body")
        if body is None:
            return feeds

        def _walk(node: ET.Element, folder: Optional[str] = None) -> None:
            if node.get("type") == "rss":
                feeds.append(
                    {
                        "title": node.get("title") or node.get("text", ""),
                        "feed_url": node.get("xmlUrl", ""),
                        "site_url": node.get("htmlUrl", ""),
                        "folder": folder,
                    }
                )
            else:
                folder_name = node.get("title") or node.get("text")
                for child in node:
                    _walk(child, folder=folder_name)

        for child in body:
            _walk(child)
    except ET.ParseError:
        pass
    return feeds


def _row_to_article(row: sqlite3.Row, account_name: str) -> dict:
    """Convert a DB row to a plain dict suitable for JSON output."""
    # Prefer url; fall back to externalURL (common for Feedly/sync accounts)
    url = row["url"] or (row["externalURL"] if "externalURL" in row.keys() else None)
    return {
        "account": account_name,
        "articleID": row["articleID"],
        "feedID": row["feedID"],
        "title": row["title"],
        "url": url,
        "summary": row["summary"],
        "datePublished": row["datePublished"],
        "dateArrived": row["dateArrived"],
        "read": bool(row["read"]),
        "starred": bool(row["starred"]),
    }


# ── Feed URL helpers ─────────────────────────────────────────────────────────

def _normalize_feed_url(feed_id: str) -> str:
    """
    Return the plain RSS URL from a feedID, regardless of account type.

    Feedly stores feedIDs as  'feed/https://example.com/rss'
    On My Mac stores them as  'https://example.com/rss'
    """
    if feed_id.startswith("feed/"):
        return feed_id[len("feed/"):]
    return feed_id


def _feedparser_entry_to_dict(entry: dict) -> dict:
    """Normalise a feedparser entry into a consistent dict."""
    published_ts: Optional[float] = None
    if entry.get("published_parsed"):
        published_ts = datetime(*entry["published_parsed"][:6]).timestamp()
    elif entry.get("updated_parsed"):
        published_ts = datetime(*entry["updated_parsed"][:6]).timestamp()

    return {
        "title": entry.get("title"),
        "url": entry.get("link"),
        "summary": entry.get("summary"),
        "datePublished": published_ts,
        "authors": [a.get("name") for a in entry.get("authors", []) if a.get("name")],
    }


# ── MCP server ───────────────────────────────────────────────────────────────

mcp = FastMCP(
    "NetNewsWire",
    instructions=(
        "Provides read-only access to the user's NetNewsWire RSS reader. "
        "Use list_feeds to see subscriptions, then get_unread_articles, "
        "get_today_articles, search_articles, or get_article_content."
    ),
)


@mcp.tool()
def list_accounts() -> str:
    """List all NetNewsWire accounts (e.g. 'On My Mac', Feedly, Feedbin) with feed counts."""
    accounts = _discover_accounts()
    if not accounts:
        return "No NetNewsWire accounts found. Is the app installed?"

    result = []
    for acc in accounts:
        feed_count = len(_parse_opml(acc["opml_path"])) if acc["opml_path"] else 0
        result.append({"account": acc["name"], "folder": acc["folder"], "feed_count": feed_count})

    return json.dumps(result, indent=2)


@mcp.tool()
def list_feeds(account_folder: Optional[str] = None) -> str:
    """
    List RSS feeds the user subscribes to, grouped by folder.

    Args:
        account_folder: Limit to a specific account folder name (e.g. 'OnMyMac').
                        Omit to list feeds from all accounts.
    """
    accounts = _discover_accounts()
    all_feeds: list[dict] = []

    for acc in accounts:
        if account_folder and acc["folder"] != account_folder:
            continue
        if acc["opml_path"]:
            for feed in _parse_opml(acc["opml_path"]):
                feed["account"] = acc["name"]
                all_feeds.append(feed)

    if not all_feeds:
        return "No feeds found."
    return json.dumps(all_feeds, indent=2)


@mcp.tool()
def get_unread_articles(
    limit: int = 20,
    feed_url: Optional[str] = None,
    account_folder: Optional[str] = None,
) -> str:
    """
    Get unread articles, newest first.

    Args:
        limit:          Max articles to return (default 20, max 100).
        feed_url:       Filter to a single feed's RSS URL (xmlUrl from list_feeds).
        account_folder: Filter to a specific account folder (e.g. 'OnMyMac').
    """
    limit = min(max(1, limit), 100)
    accounts = _discover_accounts()
    articles: list[dict] = []

    for acc in accounts:
        if account_folder and acc["folder"] != account_folder:
            continue
        try:
            conn = _open_db(acc["db_path"])
            sql = """
                SELECT a.articleID, a.feedID, a.title, a.url, a.externalURL, a.summary,
                       a.datePublished, s.dateArrived, s.read, s.starred
                FROM   articles a
                JOIN   statuses s ON a.articleID = s.articleID
                WHERE  s.read = 0
            """
            params: list = []
            if feed_url:
                sql += " AND a.feedID = ?"
                params.append(feed_url)
            sql += " ORDER BY s.dateArrived DESC LIMIT ?"
            params.append(limit)

            for row in conn.execute(sql, params).fetchall():
                articles.append(_row_to_article(row, acc["name"]))
            conn.close()
        except sqlite3.Error as exc:
            articles.append({"error": str(exc), "account": acc["name"]})

    articles.sort(key=lambda x: x.get("dateArrived") or 0, reverse=True)
    return json.dumps(articles[:limit], indent=2)


@mcp.tool()
def get_starred_articles(limit: int = 20) -> str:
    """
    Get all starred (bookmarked) articles across every account.

    Args:
        limit: Max articles to return (default 20, max 100).
    """
    limit = min(max(1, limit), 100)
    accounts = _discover_accounts()
    articles: list[dict] = []

    for acc in accounts:
        try:
            conn = _open_db(acc["db_path"])
            for row in conn.execute(
                """
                SELECT a.articleID, a.feedID, a.title, a.url, a.externalURL, a.summary,
                       a.datePublished, s.dateArrived, s.read, s.starred
                FROM   articles a
                JOIN   statuses s ON a.articleID = s.articleID
                WHERE  s.starred = 1
                ORDER BY s.dateArrived DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall():
                articles.append(_row_to_article(row, acc["name"]))
            conn.close()
        except sqlite3.Error as exc:
            articles.append({"error": str(exc), "account": acc["name"]})

    articles.sort(key=lambda x: x.get("dateArrived") or 0, reverse=True)
    return json.dumps(articles[:limit], indent=2)


@mcp.tool()
def get_today_articles(limit: int = 50) -> str:
    """
    Get articles that arrived since midnight today.

    Args:
        limit: Max articles to return (default 50, max 200).
    """
    limit = min(max(1, limit), 200)
    today_ts = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    accounts = _discover_accounts()
    articles: list[dict] = []

    for acc in accounts:
        try:
            conn = _open_db(acc["db_path"])
            for row in conn.execute(
                """
                SELECT a.articleID, a.feedID, a.title, a.url, a.externalURL, a.summary,
                       a.datePublished, s.dateArrived, s.read, s.starred
                FROM   articles a
                JOIN   statuses s ON a.articleID = s.articleID
                WHERE  s.dateArrived >= ?
                ORDER BY s.dateArrived DESC
                LIMIT ?
                """,
                (today_ts, limit),
            ).fetchall():
                articles.append(_row_to_article(row, acc["name"]))
            conn.close()
        except sqlite3.Error as exc:
            articles.append({"error": str(exc), "account": acc["name"]})

    articles.sort(key=lambda x: x.get("dateArrived") or 0, reverse=True)
    return json.dumps(articles[:limit], indent=2)


@mcp.tool()
def get_articles_by_feed(
    feed_url: str,
    limit: int = 20,
    unread_only: bool = False,
) -> str:
    """
    Get recent articles from a specific RSS feed stored in NetNewsWire.

    Accepts both plain RSS URLs and Feedly-style 'feed/https://...' IDs.
    Results are limited to what NetNewsWire has retained locally (~30-90 days).
    Use fetch_feed_live or fetch_feed_history to go further back.

    Args:
        feed_url:    The feed's RSS URL (feed_url from list_feeds), or a
                     Feedly feedID like 'feed/https://example.com/rss'.
        limit:       Max articles to return (default 20, max 100).
        unread_only: If true, return only unread articles.
    """
    limit = min(max(1, limit), 100)
    # Build both candidate feedIDs so the query works regardless of account type
    raw_url = _normalize_feed_url(feed_url)
    feedly_id = f"feed/{raw_url}"
    accounts = _discover_accounts()
    articles: list[dict] = []

    for acc in accounts:
        try:
            conn = _open_db(acc["db_path"])
            sql = """
                SELECT a.articleID, a.feedID, a.title, a.url, a.externalURL, a.summary,
                       a.datePublished, s.dateArrived, s.read, s.starred
                FROM   articles a
                JOIN   statuses s ON a.articleID = s.articleID
                WHERE  a.feedID IN (?, ?)
            """
            params: list = [raw_url, feedly_id]
            if unread_only:
                sql += " AND s.read = 0"
            sql += " ORDER BY a.datePublished DESC LIMIT ?"
            params.append(limit)

            for row in conn.execute(sql, params).fetchall():
                articles.append(_row_to_article(row, acc["name"]))
            conn.close()
        except sqlite3.Error as exc:
            articles.append({"error": str(exc), "account": acc["name"]})

    return json.dumps(articles, indent=2)


@mcp.tool()
def search_articles(query: str, limit: int = 20) -> str:
    """
    Full-text search across all article titles and body content.

    Args:
        query: Search terms (supports FTS4 syntax, e.g. 'python OR rust').
        limit: Max results to return (default 20, max 50).
    """
    limit = min(max(1, limit), 50)
    accounts = _discover_accounts()
    results: list[dict] = []

    for acc in accounts:
        try:
            conn = _open_db(acc["db_path"])
            for row in conn.execute(
                """
                SELECT a.articleID, a.feedID, a.title, a.url, a.externalURL, a.summary,
                       a.datePublished, s.dateArrived, s.read, s.starred
                FROM   articles a
                JOIN   statuses s ON a.articleID = s.articleID
                WHERE  a.searchRowID IN (
                           SELECT rowid FROM search WHERE search MATCH ?
                       )
                ORDER BY a.datePublished DESC
                LIMIT ?
                """,
                (query, limit),
            ).fetchall():
                results.append(_row_to_article(row, acc["name"]))
            conn.close()
        except sqlite3.Error as exc:
            results.append({"error": str(exc), "account": acc["name"]})

    results.sort(key=lambda x: x.get("datePublished") or 0, reverse=True)
    return json.dumps(results[:limit], indent=2)


@mcp.tool()
def get_article_content(article_id: str) -> str:
    """
    Get the full content (HTML + text) and authors for a specific article.

    Args:
        article_id: The articleID returned by other tools.
    """
    accounts = _discover_accounts()

    for acc in accounts:
        try:
            conn = _open_db(acc["db_path"])
            row = conn.execute(
                """
                SELECT a.*, s.read, s.starred, s.dateArrived
                FROM   articles a
                JOIN   statuses s ON a.articleID = s.articleID
                WHERE  a.articleID = ?
                """,
                (article_id,),
            ).fetchone()

            if row is None:
                conn.close()
                continue

            authors = conn.execute(
                """
                SELECT au.name, au.url, au.avatarURL, au.emailAddress
                FROM   authors     au
                JOIN   authorsLookup al ON au.authorID = al.authorID
                WHERE  al.articleID = ?
                """,
                (article_id,),
            ).fetchall()
            conn.close()

            return json.dumps(
                {
                    "account": acc["name"],
                    "articleID": row["articleID"],
                    "feedID": row["feedID"],
                    "title": row["title"],
                    "url": row["url"],
                    "externalURL": row["externalURL"],
                    "summary": row["summary"],
                    "contentHTML": row["contentHTML"],
                    "contentText": row["contentText"],
                    "imageURL": row["imageURL"],
                    "bannerImageURL": row["bannerImageURL"],
                    "datePublished": row["datePublished"],
                    "dateModified": row["dateModified"],
                    "dateArrived": row["dateArrived"],
                    "read": bool(row["read"]),
                    "starred": bool(row["starred"]),
                    "authors": [
                        {
                            "name": a["name"],
                            "url": a["url"],
                            "avatarURL": a["avatarURL"],
                            "email": a["emailAddress"],
                        }
                        for a in authors
                    ],
                },
                indent=2,
            )
        except sqlite3.Error as exc:
            return json.dumps({"error": str(exc)})

    return json.dumps({"error": f"Article '{article_id}' not found in any account."})


# ── Live / historical feed fetching ──────────────────────────────────────────

@mcp.tool()
def fetch_feed_live(feed_url: str, limit: int = 50) -> str:
    """
    Fetch articles directly from an RSS/Atom feed URL right now, bypassing
    NetNewsWire's local cache.  Returns whatever the feed currently publishes
    (typically the last 20-100 items depending on the feed).

    Use this when get_articles_by_feed returns nothing or you need fresher /
    slightly older items than what NetNewsWire has retained.

    Accepts both plain RSS URLs and Feedly-style 'feed/https://...' IDs.

    Args:
        feed_url: The RSS/Atom feed URL.
        limit:    Max articles to return (default 50).
    """
    limit = min(max(1, limit), 200)
    url = _normalize_feed_url(feed_url)

    feed = feedparser.parse(url)
    if feed.bozo and not feed.entries:
        return json.dumps({"error": f"Failed to parse feed: {feed.bozo_exception}"})

    articles = [_feedparser_entry_to_dict(e) for e in feed.entries[:limit]]
    return json.dumps(
        {
            "feed_title": feed.feed.get("title"),
            "feed_url": url,
            "fetched_at": datetime.now().isoformat(),
            "count": len(articles),
            "articles": articles,
        },
        indent=2,
    )


@mcp.tool()
def fetch_feed_history(
    feed_url: str,
    date: str,
    limit: int = 20,
) -> str:
    """
    Fetch an archived snapshot of a feed from the Wayback Machine (Internet
    Archive), letting you read articles published months or years ago.

    Steps: finds the closest archived copy of the RSS feed on or after the
    given date, then parses it to return the articles it contained at that time.

    Args:
        feed_url: The RSS/Atom feed URL (plain URL or Feedly 'feed/...' ID).
        date:     The target date as YYYY-MM-DD (e.g. '2024-06-01').
                  The nearest available snapshot on or after this date is used.
        limit:    Max articles to return from that snapshot (default 20).
    """
    limit = min(max(1, limit), 100)
    url = _normalize_feed_url(feed_url)

    # ── 1. Ask CDX API for the closest snapshot ───────────────────────────────
    date_compact = date.replace("-", "")  # YYYYMMDD → 20240601
    cdx_url = (
        "https://web.archive.org/cdx/search/cdx"
        f"?url={urllib.request.quote(url, safe='')}"
        f"&output=json&limit=1&fl=timestamp,statuscode"
        f"&from={date_compact}&filter=statuscode:200"
    )
    try:
        req = urllib.request.Request(
            cdx_url,
            headers={"User-Agent": "nnw-mcp/1.0 (NetNewsWire MCP history tool)"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            cdx_data = json.loads(resp.read())
    except urllib.error.URLError as exc:
        return json.dumps({"error": f"Wayback Machine CDX lookup failed: {exc}"})

    # cdx_data is [["timestamp","statuscode"], [value, value], ...]
    rows = [r for r in cdx_data if r[0] != "timestamp"]  # drop header row
    if not rows:
        return json.dumps({
            "error": f"No Wayback Machine snapshot found for '{url}' on or after {date}."
        })

    snapshot_ts = rows[0][0]  # e.g. "20240601123456"
    snapshot_url = f"https://web.archive.org/web/{snapshot_ts}id_/{url}"
    snapshot_date = f"{snapshot_ts[:4]}-{snapshot_ts[4:6]}-{snapshot_ts[6:8]}"

    # ── 2. Fetch and parse the archived feed ──────────────────────────────────
    feed = feedparser.parse(snapshot_url)
    if feed.bozo and not feed.entries:
        return json.dumps({
            "error": f"Failed to parse archived feed snapshot: {feed.bozo_exception}",
            "snapshot_url": snapshot_url,
        })

    articles = [_feedparser_entry_to_dict(e) for e in feed.entries[:limit]]
    return json.dumps(
        {
            "feed_title": feed.feed.get("title"),
            "feed_url": url,
            "requested_date": date,
            "snapshot_date": snapshot_date,
            "snapshot_url": snapshot_url,
            "count": len(articles),
            "articles": articles,
        },
        indent=2,
    )


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
