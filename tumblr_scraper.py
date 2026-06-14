"""
tumblr_scraper.py
=================
Collect Tumblr posts for a given tag via the Tumblr v2 /tagged API.

Based on bernorieder/TumblrTool (https://github.com/bernorieder/TumblrTool).

Authentication
--------------
Only a free Consumer Key (API key) is required — no OAuth / login needed for
read-only tag searches. Get one at https://www.tumblr.com/oauth/apps

Set your key via environment variable (recommended) or the --api-key CLI flag:

    export TUMBLR_API_KEY="aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789aBcD"

Usage examples
--------------
    # Collect recent posts (up to 500)
    python tumblr_scraper.py fandom --limit 500 --out results.csv

    # Collect posts within a date range
    python tumblr_scraper.py "ao3" --since 2024-01-01 --until 2024-12-31 --out ao3_2024.csv

    # Quick inspection of collected data
    python tumblr_scraper.py fandom --limit 100 --out fandom.csv --inspect

Output columns
--------------
post_id, post_type, blog_name, date, timestamp, datetime_utc, post_url,
note_count, tags, title, body_or_caption, source_url, source_title,
media_url, short_url
"""

import argparse
import csv
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tumblr_scraper")

# ── constants ─────────────────────────────────────────────────────────────────

TUMBLR_TAGGED_URL = "https://api.tumblr.com/v2/tagged"
POSTS_PER_REQUEST = 20
REQUEST_DELAY     = 0.5   # seconds between requests

CSV_COLUMNS = [
    "post_id", "post_type", "blog_name", "date", "timestamp", "datetime_utc",
    "post_url", "note_count", "tags", "title", "body_or_caption",
    "source_url", "source_title", "media_url", "short_url",
]

# ── date utilities ────────────────────────────────────────────────────────────

def parse_date(date_str: str, label: str) -> datetime:
    """Parse YYYY-MM-DD → UTC-aware datetime. Raises ValueError on bad input."""
    try:
        dt = datetime.strptime(date_str.strip(), "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"{label} '{date_str}' must be in YYYY-MM-DD format.")


def dt_to_ts(dt: datetime) -> int:
    return int(dt.timestamp())


def ts_to_dt(ts) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def to_iso(ts) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


# ── text / media utilities ────────────────────────────────────────────────────

def clean(text) -> str:
    if not text:
        return ""
    return re.sub(r"[\r\n\t]+", " ", str(text)).strip()


def extract_media_url(post: dict) -> str:
    ptype = post.get("type", "")
    if ptype == "photo":
        photos = post.get("photos", [])
        if photos:
            return photos[0].get("original_size", {}).get("url", "")
    elif ptype == "video":
        return post.get("thumbnail_url", "")
    elif ptype == "audio":
        return post.get("album_art", "")
    elif ptype == "link":
        return post.get("link_image", "")
    return ""


def extract_text_content(post: dict) -> str:
    ptype = post.get("type", "")
    if ptype == "text":
        return clean(post.get("body", ""))
    elif ptype == "quote":
        return clean(post.get("text", ""))
    elif ptype == "link":
        return clean(post.get("description", ""))
    elif ptype == "chat":
        dialogue = post.get("dialogue", [])
        return clean(" | ".join(
            f"{d.get('label', '')} {d.get('phrase', '')}" for d in dialogue
        ))
    else:
        return clean(post.get("caption", ""))


def flatten_post(post: dict) -> dict:
    tags = post.get("tags", [])
    return {
        "post_id":         post.get("id", ""),
        "post_type":       post.get("type", ""),
        "blog_name":       clean(post.get("blog_name", "")),
        "date":            post.get("date", ""),
        "timestamp":       post.get("timestamp", ""),
        "datetime_utc":    to_iso(post["timestamp"]) if post.get("timestamp") else "",
        "post_url":        post.get("post_url", ""),
        "note_count":      post.get("note_count", 0),
        "tags":            " | ".join(str(t) for t in tags),
        "title":           clean(post.get("title", "")),
        "body_or_caption": extract_text_content(post),
        "source_url":      post.get("source_url", ""),
        "source_title":    clean(post.get("source_title", "")),
        "media_url":       extract_media_url(post),
        "short_url":       post.get("short_url", ""),
    }


# ── API ───────────────────────────────────────────────────────────────────────

def fetch_page(tag: str, api_key: str, before=None) -> list:
    """Fetch one page (≤20 posts) from /tagged. Returns list of raw post dicts."""
    params = {"tag": tag, "limit": POSTS_PER_REQUEST, "api_key": api_key}
    if before is not None:
        params["before"] = before
    try:
        resp = requests.get(TUMBLR_TAGGED_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        log.warning(f"Request error: {exc} — retrying after 2 s")
        time.sleep(2)
        return []
    if data.get("meta", {}).get("status") != 200:
        log.error(f"API error: {data.get('meta', {}).get('msg', 'unknown')}")
        return []
    return data.get("response", [])


# ── scrape ────────────────────────────────────────────────────────────────────

def scrape(
    tag: str,
    api_key: str,
    limit: int = 10_000,
    since: str = None,
    until: str = None,
) -> list[dict]:
    """
    Collect posts for a tag, paginating backwards through the feed.

    Parameters
    ----------
    tag     : Tumblr tag to search (no # needed; spaces OK).
    api_key : Tumblr Consumer Key.
    limit   : Maximum posts to collect.
    since   : Earliest date (YYYY-MM-DD, inclusive).
    until   : Latest date (YYYY-MM-DD, inclusive).
    """
    date_from = parse_date(since, "since") if since else None
    date_to   = parse_date(until, "until") if until else None

    if date_from and date_to and date_from > date_to:
        raise ValueError(f"--since ({since}) must be before --until ({until})")

    ts_from = dt_to_ts(date_from) if date_from else None
    ts_to   = dt_to_ts(date_to)   if date_to   else None

    # Seed cursor just past DATE_TO so the first page is already in range
    before = (ts_to + 1) if ts_to else None

    clean_tag = tag.lstrip("#")
    if date_from or date_to:
        log.info(f'Scraping "{clean_tag}" | {since or "start"} → {until or "now"} | cap={limit}')
    else:
        log.info(f'Scraping up to {limit} posts tagged "{clean_tag}" …')

    rows     = []
    seen_ids = set()
    page     = 0

    while len(rows) < limit:
        page += 1
        cursor_label = ts_to_dt(before).strftime("%Y-%m-%d %H:%M") if before else "now"
        log.info(f"  Page {page:3d} | cursor={cursor_label} | collected: {len(rows)}")

        posts = fetch_page(clean_tag, api_key, before=before)

        if not posts:
            log.info("Empty response — no more posts available.")
            break

        new_count   = 0
        passed_from = False

        for post in posts:
            if len(rows) >= limit:
                break

            pid = post.get("id")
            if pid in seen_ids:
                continue
            seen_ids.add(pid)

            post_ts = post.get("timestamp", 0)

            # Upper bound check
            if ts_to and post_ts > ts_to:
                continue

            # Lower bound — stop when we pass date_from
            if ts_from and post_ts < ts_from:
                passed_from = True
                log.info(f"  Reached posts older than {since} — stopping.")
                break

            rows.append(flatten_post(post))
            new_count += 1

        log.info(f"         → {new_count} new posts added.")

        if passed_from:
            break

        oldest_ts = posts[-1].get("timestamp")
        if oldest_ts is None or oldest_ts == before:
            log.info("Cursor did not advance — stopping.")
            break
        before = oldest_ts

        if len(posts) < POSTS_PER_REQUEST:
            log.info("Partial page — likely reached end of tag feed.")
            break

        time.sleep(REQUEST_DELAY)

    log.info(f"Scrape complete. Posts collected: {len(rows)}")
    return rows


# ── save ──────────────────────────────────────────────────────────────────────

def save_csv(rows: list[dict], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    size_kb = path.stat().st_size / 1024
    print(f"✓  Saved {len(rows)} posts → {path}  ({size_kb:.1f} KB)")


# ── inspect ───────────────────────────────────────────────────────────────────

def inspect(rows: list[dict]) -> None:
    post_types  = Counter(r["post_type"] for r in rows)
    note_counts = [int(r["note_count"]) for r in rows if r["note_count"] != ""]
    all_tags    = [t.strip() for r in rows for t in r["tags"].split("|") if t.strip()]
    top_tags    = Counter(t.lower() for t in all_tags).most_common(10)

    print(f"\nTotal posts      : {len(rows)}")
    print(f"Post type counts : {dict(post_types)}")
    if note_counts:
        print(f"Avg note count   : {sum(note_counts)/len(note_counts):.0f}")
        print(f"Max note count   : {max(note_counts):,}")
    if rows:
        earliest = min(rows, key=lambda r: r["timestamp"] or 9e18)
        latest   = max(rows, key=lambda r: r["timestamp"] or 0)
        print(f"Earliest post    : {str(earliest['datetime_utc'])[:10]}")
        print(f"Latest post      : {str(latest['datetime_utc'])[:10]}")
    print(f"\nTop 10 co-occurring tags:")
    for tag_name, count in top_tags:
        print(f"  {count:5d}  {tag_name}")

    print(f"\n{'─'*70}")
    for i, row in enumerate(rows[:3], 1):
        print(f"\n--- Post {i} ---")
        for col in CSV_COLUMNS:
            val = str(row.get(col, ""))
            print(f"  {col:<18} {val[:100]}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_output_path(tag: str, since: str, until: str, limit: int) -> Path:
    safe_tag = re.sub(r"[^\w\-]", "_", tag.lower()).strip("_")
    run_ts   = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    if since or until:
        from_str = since.replace("-", "") if since else "start"
        to_str   = until.replace("-", "") if until else "now"
        return Path(f"tumblr_{safe_tag}_{from_str}_to_{to_str}_{run_ts}.csv")
    return Path(f"tumblr_{safe_tag}_{limit}posts_{run_ts}.csv")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect Tumblr posts by tag via the v2 /tagged API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("tag", help="Tag to search (no # needed)")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("TUMBLR_API_KEY", ""),
        help="Tumblr Consumer Key (or set TUMBLR_API_KEY env var)",
    )
    parser.add_argument("--limit",   type=int, default=10_000, help="Max posts to collect (default: 10000)")
    parser.add_argument("--since",   default=None, help="Start date YYYY-MM-DD (inclusive)")
    parser.add_argument("--until",   default=None, help="End date YYYY-MM-DD (inclusive)")
    parser.add_argument("--out",     default=None,  help="Output CSV path (auto-generated if omitted)")
    parser.add_argument("--inspect", action="store_true", help="Print summary stats after scraping")

    args = parser.parse_args()

    if not args.api_key:
        parser.error(
            "No API key found. Set TUMBLR_API_KEY as an environment variable "
            "or pass --api-key YOUR_KEY.\n"
            "Get a free key at https://www.tumblr.com/oauth/apps"
        )

    out_path = Path(args.out) if args.out else build_output_path(
        args.tag, args.since, args.until, args.limit
    )

    rows = scrape(
        tag=args.tag,
        api_key=args.api_key,
        limit=args.limit,
        since=args.since,
        until=args.until,
    )

    if rows:
        save_csv(rows, out_path)
        if args.inspect:
            inspect(rows)
    else:
        print("No posts collected.")
        sys.exit(1)


if __name__ == "__main__":
    main()
