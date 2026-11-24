"""
tumblr_url_scraper.py
=====================
Collect specific Tumblr posts from a list of post URLs via the Tumblr v2 API,
strip all HTML from the text fields, and save the results to CSV.

Companion to tumblr_scraper.py (which collects posts by tag).

Authentication
--------------
Only a free Consumer Key (API key) is required. Get one at
https://www.tumblr.com/oauth/apps and set it via environment variable
(recommended) or the --api-key CLI flag:

    export TUMBLR_API_KEY="aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789aBcD"

Supported URL formats
---------------------
    https://www.tumblr.com/{blog}/{post_id}[/slug]
    https://tumblr.com/{blog}/{post_id}[/slug]
    https://www.tumblr.com/blog/view/{blog}/{post_id}
    https://{blog}.tumblr.com/post/{post_id}[/slug]
    https://{custom-domain}/post/{post_id}[/slug]
    https://tmblr.co/...  (short links — resolved by following the redirect)

Usage examples
--------------
    # URLs from a text file (one per line; blank lines and # comments ignored)
    python tumblr_url_scraper.py --input urls.txt --out posts.csv

    # URLs from a CSV file column (default column name: "url")
    python tumblr_url_scraper.py --input my_list.csv --url-column post_url

    # URLs passed directly on the command line
    python tumblr_url_scraper.py https://www.tumblr.com/staff/123456789 \\
        https://example.tumblr.com/post/987654321/some-slug

Output columns
--------------
input_url, status, error, post_id, post_type, blog_name, date, timestamp,
datetime_utc, post_url, note_count, tags, title, body_or_caption,
source_url, source_title, media_url, short_url

All text columns are plain text: HTML tags are removed, entities decoded
(&amp; → &), and whitespace collapsed to single spaces.
"""

import argparse
import csv
import html
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import requests

# ── logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tumblr_url_scraper")

# ── constants ─────────────────────────────────────────────────────────────────

TUMBLR_POSTS_URL = "https://api.tumblr.com/v2/blog/{blog}/posts"
REQUEST_DELAY    = 0.5   # seconds between requests
MAX_RETRIES      = 3     # retries on network errors / HTTP 429 / 5xx

CSV_COLUMNS = [
    "input_url", "status", "error",
    "post_id", "post_type", "blog_name", "date", "timestamp", "datetime_utc",
    "post_url", "note_count", "tags", "title", "body_or_caption",
    "source_url", "source_title", "media_url", "short_url",
]

# ── HTML stripping ────────────────────────────────────────────────────────────

# Tags that should introduce a word break so "<p>a</p><p>b</p>" → "a b", not "ab"
_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table",
    "td", "th", "tr", "ul",
}
_SKIP_TAGS = {"script", "style", "noscript"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def strip_html(text) -> str:
    """Remove HTML tags, decode entities, and collapse whitespace."""
    if not text:
        return ""
    parser = _TextExtractor()
    parser.feed(str(text))
    parser.close()
    out = html.unescape("".join(parser.parts))   # catch any double-encoded entities
    out = out.replace("\xa0", " ")
    return re.sub(r"\s+", " ", out).strip()


# ── URL parsing ───────────────────────────────────────────────────────────────

_TUMBLR_HOSTS = {"tumblr.com", "www.tumblr.com"}
_SHORT_HOSTS  = {"tmblr.co", "www.tmblr.co", "tumblr.co", "t.umblr.com"}


def parse_tumblr_url(url: str):
    """
    Return (blog_identifier, post_id) for a Tumblr post URL, or None if the
    URL is not in a recognised format.
    """
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parsed = urlparse(url)
    host   = (parsed.hostname or "").lower()
    parts  = [p for p in parsed.path.split("/") if p]

    if host in _TUMBLR_HOSTS:
        # /blog/view/{blog}/{id}
        if len(parts) >= 4 and parts[0] == "blog" and parts[1] == "view" and parts[3].isdigit():
            return parts[2], parts[3]
        # /{blog}/{id}[/slug]
        if len(parts) >= 2 and parts[1].isdigit():
            return parts[0], parts[1]
        return None

    # {blog}.tumblr.com/post/{id} or {custom-domain}/post/{id}
    if len(parts) >= 2 and parts[0] == "post" and parts[1].isdigit():
        if host.endswith(".tumblr.com"):
            return host[: -len(".tumblr.com")], parts[1]
        return host, parts[1]   # custom domain — the API accepts hostnames

    return None


def resolve_short_url(url: str) -> str:
    """Follow redirects for tmblr.co-style short links. Returns the final URL."""
    try:
        resp = requests.head(url, allow_redirects=True, timeout=15)
        return resp.url
    except requests.RequestException as exc:
        log.warning(f"Could not resolve short URL {url}: {exc}")
        return url


def is_short_url(url: str) -> bool:
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return (urlparse(url).hostname or "").lower() in _SHORT_HOSTS


# ── text / media utilities ────────────────────────────────────────────────────

def to_iso(ts) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


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
        return strip_html(post.get("body", ""))
    elif ptype == "quote":
        text   = strip_html(post.get("text", ""))
        source = strip_html(post.get("source", ""))
        return f"{text} — {source}" if source else text
    elif ptype == "link":
        return strip_html(post.get("description", ""))
    elif ptype == "chat":
        dialogue = post.get("dialogue", [])
        return strip_html(" | ".join(
            f"{d.get('label', '')} {d.get('phrase', '')}" for d in dialogue
        ))
    elif ptype == "answer":
        q = strip_html(post.get("question", ""))
        a = strip_html(post.get("answer", ""))
        asker = post.get("asking_name", "")
        prefix = f"Q ({asker}): " if asker else "Q: "
        return f"{prefix}{q} A: {a}".strip()
    else:
        return strip_html(post.get("caption", ""))


def flatten_post(post: dict, input_url: str) -> dict:
    tags = post.get("tags", [])
    return {
        "input_url":       input_url,
        "status":          "ok",
        "error":           "",
        "post_id":         post.get("id_string") or post.get("id", ""),
        "post_type":       post.get("type", ""),
        "blog_name":       strip_html(post.get("blog_name", "")),
        "date":            post.get("date", ""),
        "timestamp":       post.get("timestamp", ""),
        "datetime_utc":    to_iso(post["timestamp"]) if post.get("timestamp") else "",
        "post_url":        post.get("post_url", ""),
        "note_count":      post.get("note_count", 0),
        "tags":            " | ".join(strip_html(t) for t in tags),
        "title":           strip_html(post.get("title", "")),
        "body_or_caption": extract_text_content(post),
        "source_url":      post.get("source_url", ""),
        "source_title":    strip_html(post.get("source_title", "")),
        "media_url":       extract_media_url(post),
        "short_url":       post.get("short_url", ""),
    }


def error_row(input_url: str, status: str, error: str) -> dict:
    row = {col: "" for col in CSV_COLUMNS}
    row.update(input_url=input_url, status=status, error=error)
    return row


# ── API ───────────────────────────────────────────────────────────────────────

def fetch_post(blog: str, post_id: str, api_key: str):
    """
    Fetch a single post. Returns (post_dict, None) on success or
    (None, error_message) on failure.
    """
    url    = TUMBLR_POSTS_URL.format(blog=blog)
    params = {"id": post_id, "api_key": api_key}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=15)
        except requests.RequestException as exc:
            # Exception text includes the request URL — keep the key out of logs/CSV
            err = f"network error: {exc}".replace(api_key, "***")
        else:
            if resp.status_code == 429 or resp.status_code >= 500:
                err = f"HTTP {resp.status_code}"
            else:
                try:
                    data = resp.json()
                except ValueError:
                    return None, f"HTTP {resp.status_code}: non-JSON response"
                meta = data.get("meta", {})
                if meta.get("status") != 200:
                    return None, f"API {meta.get('status', resp.status_code)}: {meta.get('msg', 'unknown error')}"
                posts = data.get("response", {}).get("posts", [])
                if not posts:
                    return None, "post not found (deleted, private, or wrong ID)"
                return posts[0], None

        if attempt < MAX_RETRIES:
            wait = 2 ** attempt
            log.warning(f"    {err} — retrying in {wait} s ({attempt}/{MAX_RETRIES})")
            time.sleep(wait)

    return None, err


# ── scrape ────────────────────────────────────────────────────────────────────

def scrape_urls(urls: list[str], api_key: str) -> list[dict]:
    """
    Fetch each Tumblr post URL and return one flattened row per input URL.
    Failed URLs are kept as rows with status != "ok" and an error message,
    so the output always lines up with the input list.
    """
    # De-duplicate while preserving order
    seen, unique = set(), []
    for u in urls:
        u = u.strip()
        if u and u not in seen:
            seen.add(u)
            unique.append(u)
    if len(unique) < len(urls):
        log.info(f"Removed {len(urls) - len(unique)} blank/duplicate URLs.")

    log.info(f"Fetching {len(unique)} posts …")
    rows = []

    for i, input_url in enumerate(unique, 1):
        target = resolve_short_url(input_url) if is_short_url(input_url) else input_url
        parsed = parse_tumblr_url(target)

        if not parsed:
            log.warning(f"  [{i}/{len(unique)}] Unrecognised URL format: {input_url}")
            rows.append(error_row(input_url, "invalid_url", "could not parse blog name / post ID"))
            continue

        blog, post_id = parsed
        log.info(f"  [{i}/{len(unique)}] {blog} / {post_id}")
        post, err = fetch_post(blog, post_id, api_key)

        if post:
            rows.append(flatten_post(post, input_url))
        else:
            log.warning(f"    ✗ {err}")
            rows.append(error_row(input_url, "error", err))

        if i < len(unique):
            time.sleep(REQUEST_DELAY)

    ok = sum(r["status"] == "ok" for r in rows)
    log.info(f"Done. {ok}/{len(rows)} posts collected successfully.")
    return rows


# ── input / output ────────────────────────────────────────────────────────────

def load_urls(path: Path, url_column: str = "url") -> list[str]:
    """
    Load URLs from a .csv (reads `url_column`) or a plain-text file
    (one URL per line; blank lines and lines starting with # are ignored).
    """
    if path.suffix.lower() == ".csv":
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if url_column not in (reader.fieldnames or []):
                raise ValueError(
                    f"Column '{url_column}' not found in {path}. "
                    f"Available columns: {reader.fieldnames}"
                )
            return [r[url_column].strip() for r in reader if (r.get(url_column) or "").strip()]

    with open(path, encoding="utf-8-sig") as f:
        return [
            line.strip() for line in f
            if line.strip() and not line.strip().startswith("#")
        ]


def save_csv(rows: list[dict], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    size_kb = path.stat().st_size / 1024
    print(f"✓  Saved {len(rows)} rows → {path}  ({size_kb:.1f} KB)")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect Tumblr posts from a list of URLs and save them as HTML-free CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("urls", nargs="*", help="Tumblr post URLs (optional if --input is used)")
    parser.add_argument("--input", "-i", type=Path, default=None,
                        help="File of URLs: .txt (one per line) or .csv (see --url-column)")
    parser.add_argument("--url-column", default="url",
                        help='Column holding URLs when --input is a CSV (default: "url")')
    parser.add_argument(
        "--api-key",
        default=os.environ.get("TUMBLR_API_KEY", ""),
        help="Tumblr Consumer Key (or set TUMBLR_API_KEY env var)",
    )
    parser.add_argument("--out", default=None, help="Output CSV path (auto-generated if omitted)")

    args = parser.parse_args()

    if not args.api_key:
        parser.error(
            "No API key found. Set TUMBLR_API_KEY as an environment variable "
            "or pass --api-key YOUR_KEY.\n"
            "Get a free key at https://www.tumblr.com/oauth/apps"
        )

    urls = list(args.urls)
    if args.input:
        urls += load_urls(args.input, args.url_column)
    if not urls:
        parser.error("No URLs given. Pass URLs as arguments or use --input FILE.")

    run_ts   = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.out) if args.out else Path(f"tumblr_urls_{run_ts}.csv")

    rows = scrape_urls(urls, args.api_key)
    save_csv(rows, out_path)

    if not any(r["status"] == "ok" for r in rows):
        print("No posts collected successfully.")
        sys.exit(1)


if __name__ == "__main__":
    main()
