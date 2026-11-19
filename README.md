# Tumblr Tag Scraper

Collect public Tumblr posts for a given tag via the [Tumblr v2 `/tagged` API](https://www.tumblr.com/docs/en/api/v2#tagged-method).

Based on [bernorieder/TumblrTool](https://github.com/bernorieder/TumblrTool).

---

## Requirements

```bash
pip install requests
```

Python 3.10+.

---

## Authentication

Only a free **Consumer Key** (API key) is required — no OAuth or login needed for read-only tag searches.

1. Go to [tumblr.com/oauth/apps](https://www.tumblr.com/oauth/apps) → *Register application*
2. Copy the **Consumer Key**
3. Set it as an environment variable (recommended) or pass it via CLI flag

```bash
export TUMBLR_API_KEY="aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789aBcD"
```

---

## Usage

### Command line

```bash
# Collect up to 500 recent posts tagged "fandom"
python tumblr_scraper.py fandom --limit 500 --out fandom.csv

# Collect posts within a date range
python tumblr_scraper.py "ao3" --since 2024-01-01 --until 2024-12-31 --out ao3_2024.csv

# Collect and print summary stats
python tumblr_scraper.py "fan fiction" --limit 1000 --out fanfic.csv --inspect

# Pass API key directly
python tumblr_scraper.py fandom --api-key YOUR_KEY --limit 200 --out out.csv
```

If `--out` is omitted, the filename is auto-generated from the tag, date range, and run timestamp — e.g. `tumblr_ao3_20240101_to_20241231_20250101_120000.csv`.

### Date range options

| Flags | Behavior |
|---|---|
| `--since` and `--until` | Collect within that window |
| `--until` only | Collect everything up to that date |
| `--since` only | Collect everything from that date forward |
| Neither | Collect the most recent posts up to `--limit` |

Dates are **inclusive** and interpreted as UTC midnight.

### As a Python library

```python
from tumblr_scraper import scrape, save_csv, inspect
from pathlib import Path

rows = scrape(
    tag="ao3",
    api_key="YOUR_KEY",
    limit=5000,
    since="2024-01-01",
    until="2024-06-30",
)

save_csv(rows, Path("ao3_h1_2024.csv"))
inspect(rows)   # prints summary stats
```

---

## Output columns

| Column | Description |
|---|---|
| `post_id` | Tumblr post ID |
| `post_type` | text, photo, quote, link, chat, audio, video |
| `blog_name` | Author's blog name |
| `date` | Post date string from API |
| `timestamp` | Unix timestamp |
| `datetime_utc` | ISO-8601 UTC datetime |
| `post_url` | Permalink |
| `note_count` | Likes + reblogs |
| `tags` | Pipe-separated tag list |
| `title` | Post title (where applicable) |
| `body_or_caption` | Text content or caption, whitespace-normalised |
| `source_url` | Original source URL (reblogs) |
| `source_title` | Original source title (reblogs) |
| `media_url` | Primary media URL (photo, video, audio) |
| `short_url` | Tumblr short URL |

---

## Notebook

`tumblr_scraper.ipynb` provides the same functionality in an interactive Jupyter environment with step-by-step cells, inline progress logging, and a quick-inspection section (post type breakdown, top co-occurring tags, sample posts).

---

## Scraping a list of URLs

`tumblr_url_scraper.py` / `tumblr_url_scraper.ipynb` collect **specific posts** from a list of Tumblr post URLs instead of a tag. They use the same API key. All HTML is removed from text fields: tags are stripped, entities are decoded, and whitespace is collapsed.

```bash
# URLs from a text file (one per line; blank lines and # comments ignored)
python tumblr_url_scraper.py --input urls.txt --out posts.csv

# URLs from a column of a CSV file (default column: "url")
python tumblr_url_scraper.py --input my_list.csv --url-column post_url

# URLs directly on the command line
python tumblr_url_scraper.py https://www.tumblr.com/staff/123456789 https://example.tumblr.com/post/987654321/slug
```

Supported URL formats: `tumblr.com/{blog}/{id}`, `tumblr.com/blog/view/{blog}/{id}`, `{blog}.tumblr.com/post/{id}`, custom domains (`example.com/post/{id}`), and `tmblr.co` short links.

The output has the same columns as the tag scraper, plus three at the front:

| Column | Description |
|---|---|
| `input_url` | The URL as given in your list |
| `status` | `ok`, `error` (API/network failure, deleted or private post), or `invalid_url` |
| `error` | Reason for failure, empty when `status` is `ok` |

Every input URL produces exactly one row. Duplicate URLs are dropped. Ask posts are written to `body_or_caption` as `Q (asker): … A: …`.

---

## Notes

- The `/tagged` endpoint paginates **backwards** through time using timestamps. Date-bounded runs stop automatically when the lower bound is reached, so `--since` / `--until` is substantially faster than collecting everything and filtering.
- The scraper respects the API's rate limits with a configurable delay between requests (`REQUEST_DELAY`, default 0.5 s).
- Tags with spaces are supported; the `#` prefix is stripped automatically.
- Only **public** posts are accessible via the Consumer Key flow.

---

## Ethics and data use

Data collected is public, but consider your IRB or ethics requirements before publishing datasets that include blog names or post content. Be a good citizen: collect only what you need and avoid hammering the API.

---

## License

MIT
