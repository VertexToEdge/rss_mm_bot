import json
import logging
import os
import re
import time
from pathlib import Path

import feedparser
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

GEEKNEWS_FEED_URL = "https://feeds.feedburner.com/geeknews-feed"
HN_API_BASE = "https://hacker-news.firebaseio.com/v0"
HN_STATE_KEY = "hackernews_top"
HN_MIN_SCORE_COMMENTS = int(os.environ.get("HN_MIN_SCORE_COMMENTS", "100"))

GEEKNEWS_WEBHOOK = os.environ.get("GEEKNEWS_WEBHOOK_URL", "")
HACKERNEWS_WEBHOOK = os.environ.get("HACKERNEWS_WEBHOOK_URL", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
STATE_FILE = DATA_DIR / "sent.json"
MAX_IDS_PER_FEED = 1000


# --- State ---

def load_state() -> dict[str, list[str]]:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to load state file, starting fresh: %s", e)
    return {}


def save_state(state: dict[str, list[str]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)


# --- Mattermost ---

def send_to_mattermost(webhook_url: str, text: str) -> bool:
    try:
        resp = requests.post(
            webhook_url,
            json={"text": text, "username": "RSS_BOT"},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error("Failed to send to Mattermost: %s", e)
        return False


# --- GeekNews (RSS) ---

def strip_html(html: str) -> str:
    text = re.sub(r"<li>\s*", "- ", html)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def format_geeknews(entry) -> str:
    title = entry.get("title", "(no title)")
    link = entry.get("link", "")
    author = entry.get("author", "")
    published = entry.get("published", "")

    content_html = ""
    if entry.get("content"):
        content_html = entry["content"][0].get("value", "")
    elif entry.get("summary"):
        content_html = entry["summary"]

    summary = strip_html(content_html)

    lines = [f"#### [{title}]({link})"]
    if summary:
        lines.append(f"\n{summary}")
    meta = []
    if author:
        meta.append(f"by **{author}**")
    if published:
        meta.append(published)
    if meta:
        lines.append(f"\n> {' | '.join(meta)}")

    return "\n".join(lines)


def poll_geeknews(state: dict[str, list[str]]) -> None:
    try:
        feed = feedparser.parse(GEEKNEWS_FEED_URL)
    except Exception as e:
        log.error("Failed to fetch GeekNews feed: %s", e)
        return

    if feed.bozo and not feed.entries:
        log.warning("GeekNews feed error: %s", feed.bozo_exception)
        return

    first_run = GEEKNEWS_FEED_URL not in state
    seen_list = state.get(GEEKNEWS_FEED_URL, [])
    seen_set = set(seen_list)

    new_entries = [e for e in feed.entries
                   if (e.get("id") or e.get("link", "")) not in seen_set]

    if not new_entries:
        log.info("No new entries from GeekNews")
        return

    if first_run:
        log.info("First run: marking %d GeekNews entries as seen", len(new_entries))
        for entry in new_entries:
            seen_list.append(entry.get("id") or entry.get("link", ""))
        state[GEEKNEWS_FEED_URL] = seen_list[-MAX_IDS_PER_FEED:]
        return

    log.info("Found %d new entries from GeekNews", len(new_entries))
    for entry in reversed(new_entries):
        entry_id = entry.get("id") or entry.get("link", "")
        text = format_geeknews(entry)
        if send_to_mattermost(GEEKNEWS_WEBHOOK, text):
            seen_list.append(entry_id)

    state[GEEKNEWS_FEED_URL] = seen_list[-MAX_IDS_PER_FEED:]


# --- Hacker News (API) ---

def fetch_hn_top_ids() -> list[int]:
    try:
        resp = requests.get(f"{HN_API_BASE}/topstories.json", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        log.error("Failed to fetch HN top stories: %s", e)
        return []


def fetch_hn_item(item_id: int) -> dict | None:
    try:
        resp = requests.get(f"{HN_API_BASE}/item/{item_id}.json", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        log.error("Failed to fetch HN item %d: %s", item_id, e)
        return None


def format_hackernews(item: dict) -> str:
    title = item.get("title", "(no title)")
    url = item.get("url", "")
    author = item.get("by", "")
    score = item.get("score", 0)
    descendants = item.get("descendants", 0)
    item_id = item.get("id", 0)
    comments_url = f"https://news.ycombinator.com/item?id={item_id}"

    # If no external URL (e.g. Ask HN), link to HN page
    link = url or comments_url

    lines = [f"#### [{title}]({link})"]

    meta = []
    meta.append(f"{score} points")
    if author:
        meta.append(f"by **{author}**")
    meta.append(f"[{descendants} comments]({comments_url})")
    lines.append(f"> {' | '.join(meta)}")

    return "\n".join(lines)


def poll_hackernews(state: dict[str, list[str]]) -> None:
    top_ids = fetch_hn_top_ids()
    if not top_ids:
        return

    first_run = HN_STATE_KEY not in state
    seen_list = state.get(HN_STATE_KEY, [])
    seen_set = set(seen_list)

    new_ids = [str(sid) for sid in top_ids if str(sid) not in seen_set]

    if not new_ids:
        log.info("No new entries from Hacker News")
        return

    if first_run:
        log.info("First run: marking %d HN top stories as seen", len(new_ids))
        seen_list.extend(new_ids)
        state[HN_STATE_KEY] = seen_list[-MAX_IDS_PER_FEED:]
        return

    log.info("Found %d new top stories from Hacker News", len(new_ids))
    for str_id in reversed(new_ids):
        item = fetch_hn_item(int(str_id))
        if not item or item.get("dead") or item.get("deleted"):
            seen_list.append(str_id)
            continue
        score = item.get("score", 0)
        descendants = item.get("descendants", 0)
        if score + descendants < HN_MIN_SCORE_COMMENTS:
            log.debug("HN item %s skipped (score=%d, comments=%d, sum=%d < %d)",
                       str_id, score, descendants, score + descendants, HN_MIN_SCORE_COMMENTS)
            continue
        text = format_hackernews(item)
        if send_to_mattermost(HACKERNEWS_WEBHOOK, text):
            seen_list.append(str_id)

    state[HN_STATE_KEY] = seen_list[-MAX_IDS_PER_FEED:]


# --- Main ---

def poll_once(state: dict[str, list[str]]) -> None:
    if GEEKNEWS_WEBHOOK:
        poll_geeknews(state)
    if HACKERNEWS_WEBHOOK:
        poll_hackernews(state)
    save_state(state)


def run() -> None:
    if not GEEKNEWS_WEBHOOK and not HACKERNEWS_WEBHOOK:
        log.error("At least one of GEEKNEWS_WEBHOOK_URL or "
                  "HACKERNEWS_WEBHOOK_URL must be set")
        raise SystemExit(1)

    active = []
    if GEEKNEWS_WEBHOOK:
        active.append("GeekNews")
    if HACKERNEWS_WEBHOOK:
        active.append("Hacker News")
    log.info("RSS bot starting (interval=%ds, feeds=%s)",
             POLL_INTERVAL, ", ".join(active))

    state = load_state()

    while True:
        try:
            poll_once(state)
        except Exception:
            log.exception("Unexpected error during polling")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
