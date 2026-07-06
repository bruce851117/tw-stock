import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


ENTRY_URL = "https://socialworkerdaily.com/index/invest/notes-of-gooaye/"
SOURCE_NAME = "股癌筆記"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")

DATA_DIR = Path("data")
RAW_OUTPUT_PATH = DATA_DIR / "gooaye_latest_raw.json"
STATE_PATH = DATA_DIR / "gooaye_state.json"
DISPLAY_OUTPUT_PATH = DATA_DIR / "gooaye_news.json"
HISTORY_PATH = DATA_DIR / "gooaye_history.json"

LATEST_N = 5
MAX_RANGE_PAGES = 20
REQUEST_SLEEP_SECONDS = 0.4


def now_taipei_string():
    return datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S")


def fetch_html(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    }

    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def save_json(path, data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_json(path, default):
    if not path.exists():
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def clean_text(text):
    if not text:
        return ""

    lines = []
    for line in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)

    return "\n".join(lines).strip()

def cut_content_before_thoughts(content):
    if not content:
        return ""

    markers = [
        "二、心得感想",
        "二、 心得感想",
        "二、心得",
        "貳、心得感想",
        "貳、 心得感想",
        "貳、心得",
        "二. 心得感想",
        "二、心得感想：",
        "貳、心得感想："
    ]

    cut_positions = []

    for marker in markers:
        index = content.find(marker)

        if index >= 0:
            cut_positions.append(index)

    if not cut_positions:
        return content.strip()

    cut_index = min(cut_positions)
    return content[:cut_index].strip()

def get_meta_content(soup, property_name=None, name=None):
    tag = None

    if property_name:
        tag = soup.find("meta", attrs={"property": property_name})

    if not tag and name:
        tag = soup.find("meta", attrs={"name": name})

    if not tag:
        return ""

    return tag.get("content", "").strip()


def iso_to_taipei(value):
    if not value:
        return ""

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def get_title(soup):
    title = get_meta_content(soup, property_name="og:title")
    if title:
        return clean_text(title)

    h1 = soup.find("h1")
    if h1:
        return clean_text(h1.get_text(" ", strip=True))

    if soup.title:
        return clean_text(soup.title.get_text(" ", strip=True))

    return ""


def same_domain(url_a, url_b):
    return urlparse(url_a).netloc == urlparse(url_b).netloc


def discover_latest_range():
    print(f"Open Gooaye entry: {ENTRY_URL}", flush=True)

    html = fetch_html(ENTRY_URL)
    soup = BeautifulSoup(html, "html.parser")

    candidates = []

    for a in soup.find_all("a", href=True):
        href = urljoin(ENTRY_URL, a.get("href", ""))
        text = clean_text(a.get_text(" ", strip=True))

        low = None
        high = None

        url_match = re.search(r"ep-(\d+)-to-(\d+)", href)
        text_match = re.search(r"(\d+)\s*集\s*[-~～到至]\s*(\d+)\s*集", text)

        if url_match:
            low = int(url_match.group(1))
            high = int(url_match.group(2))
        elif text_match:
            low = int(text_match.group(1))
            high = int(text_match.group(2))

        if low is not None and high is not None:
            candidates.append({
                "low": low,
                "high": high,
                "text": text,
                "url": href,
            })

    if not candidates:
        raise RuntimeError("No Gooaye episode range links found from entry page.")

    candidates.sort(key=lambda item: (item["high"], item["low"]), reverse=True)
    latest = candidates[0]

    print(
        f"Latest range: {latest['low']} to {latest['high']} | {latest['url']}",
        flush=True,
    )

    return latest


def extract_episode_from_link(href, text):
    patterns = [
        r"notes-of-gooaye-ep-(\d+)",
        r"gooaye-ep-(\d+)",
        r"EP\s*(\d+)",
        r"Ep\s*(\d+)",
        r"ep\s*(\d+)",
    ]

    joined = f"{href} {text}"

    for pattern in patterns:
        m = re.search(pattern, joined, flags=re.IGNORECASE)
        if m:
            return int(m.group(1))

    return None


def is_range_pagination_url(url, range_url):
    if not same_domain(url, range_url):
        return False

    parsed_url = urlparse(url)
    parsed_range = urlparse(range_url)

    range_path = parsed_range.path.rstrip("/")
    url_path = parsed_url.path.rstrip("/")

    if range_path not in url_path:
        return False

    if "notes-of-gooaye-ep-" in url_path:
        return False

    return True


def collect_episode_links(range_url):
    to_visit = [range_url]
    visited = set()
    episode_map = {}

    while to_visit and len(visited) < MAX_RANGE_PAGES:
        page_url = to_visit.pop(0)

        if page_url in visited:
            continue

        visited.add(page_url)

        print(f"Open Gooaye range page: {page_url}", flush=True)

        html = fetch_html(page_url)
        soup = BeautifulSoup(html, "html.parser")

        for a in soup.find_all("a", href=True):
            href = urljoin(page_url, a.get("href", ""))
            text = clean_text(a.get_text(" ", strip=True))

            episode = extract_episode_from_link(href, text)

            if episode is not None and "notes-of-gooaye" in href:
                old = episode_map.get(episode)

                if not old:
                    episode_map[episode] = {
                        "episode": episode,
                        "title": text or f"股癌筆記EP{episode}",
                        "url": href.split("#")[0],
                    }

            if is_range_pagination_url(href, range_url):
                href = href.split("#")[0]

                # 網站常見分頁可能是 /page/2/，也可能是頁碼按鈕或「下一頁」。
                if href not in visited and href not in to_visit:
                    if (
                        re.search(r"/page/\d+/?", href)
                        or re.search(r"[?&]paged=\d+", href)
                        or text.isdigit()
                        or "下一頁" in text
                    ):
                        to_visit.append(href)

        time.sleep(REQUEST_SLEEP_SECONDS)

    if not episode_map:
        raise RuntimeError("No Gooaye episode links found from latest range page.")

    ordered = sorted(episode_map.values(), key=lambda item: item["episode"], reverse=True)

    print(
        "Episodes found: " + ", ".join(str(item["episode"]) for item in ordered[:20]),
        flush=True,
    )

    return ordered


def extract_article_content(soup):
    for tag_name in ["script", "style", "noscript", "iframe", "svg", "form"]:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    for selector in ["nav", "header", "footer", "aside"]:
        for tag in soup.find_all(selector):
            tag.decompose()

    candidates = []

    article = soup.find("article")
    if article:
        candidates.append(article)

    main = soup.find("main")
    if main:
        candidates.append(main)

    for class_name in ["entry-content", "post-content", "content", "site-content"]:
        tag = soup.find(class_=class_name)
        if tag:
            candidates.append(tag)

    if candidates:
        # 選文字最多的那個區塊
        candidates.sort(key=lambda tag: len(tag.get_text(" ", strip=True)), reverse=True)
        return clean_text(candidates[0].get_text("\n", strip=True))

    if soup.body:
        return clean_text(soup.body.get_text("\n", strip=True))

    return ""


def fetch_episode_detail(item):
    url = item["url"]
    episode = item["episode"]

    print(f"Open Gooaye EP{episode}: {url}", flush=True)

    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    title = get_title(soup) or item.get("title") or f"股癌筆記EP{episode}"

    published_raw = get_meta_content(soup, property_name="article:published_time")
    modified_raw = get_meta_content(soup, property_name="article:modified_time")

    content = extract_article_content(soup)
    content = cut_content_before_thoughts(content)

    return {
        "episode": episode,
        "title": title,
        "url": url,
        "published_raw": published_raw,
        "modified_raw": modified_raw,
        "published_at": iso_to_taipei(published_raw),
        "modified_at": iso_to_taipei(modified_raw),
        "content": content,
        "content_length": len(content),
    }


def make_display_json(raw_output):
    history = load_json(HISTORY_PATH, {"episodes": {}})
    history_episodes = history.get("episodes") or {}

    articles = []

    for item in raw_output.get("episodes", []):
        ep_key = str(item.get("episode"))
        saved = history_episodes.get(ep_key) or {}

        summary = saved.get("summary")
        key_points = saved.get("key_points", [])
        market_topics = saved.get("market_topics", [])
        stocks = saved.get("stocks", [])

        # Gemini prompt 還沒定稿前，先用原文前段當 preview。
        if not summary:
            content = item.get("content", "")
            summary = content[:800] + ("..." if len(content) > 800 else "")

        articles.append({
            "episode": item.get("episode"),
            "title": item.get("title"),
            "url": item.get("url"),
            "published_at": item.get("published_at"),
            "modified_at": item.get("modified_at"),
            "summary": summary,
            "key_points": key_points,
            "market_topics": market_topics,
            "stocks": stocks,
        })

    return {
        "source_name": SOURCE_NAME,
        "source_url": ENTRY_URL,
        "latest_range_url": raw_output.get("latest_range_url", ""),
        "updated_at": raw_output.get("fetched_at", now_taipei_string()),
        "article_count": len(articles),
        "articles": articles,
    }


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    latest_range = discover_latest_range()
    episode_links = collect_episode_links(latest_range["url"])

    latest_links = episode_links[:LATEST_N]

    episodes = []
    for item in latest_links:
        episodes.append(fetch_episode_detail(item))
        time.sleep(REQUEST_SLEEP_SECONDS)

    latest_episode_numbers = [item["episode"] for item in episodes]

    old_state = load_json(STATE_PATH, {})
    old_episode_numbers = old_state.get("latest_episodes", [])

    changed = old_episode_numbers != latest_episode_numbers

    raw_output = {
        "source_name": SOURCE_NAME,
        "source_url": ENTRY_URL,
        "latest_range": {
            "low": latest_range["low"],
            "high": latest_range["high"],
            "text": latest_range["text"],
            "url": latest_range["url"],
        },
        "latest_range_url": latest_range["url"],
        "fetched_at": now_taipei_string(),
        "timezone": "Asia/Taipei",
        "latest_n": LATEST_N,
        "latest_episodes": latest_episode_numbers,
        "changed": changed,
        "episodes": episodes,
    }

    state_output = {
        "updated_at": raw_output["fetched_at"],
        "timezone": "Asia/Taipei",
        "source_name": SOURCE_NAME,
        "source_url": ENTRY_URL,
        "latest_range_url": latest_range["url"],
        "latest_episodes": latest_episode_numbers,
        "changed": changed,
    }

    display_output = make_display_json(raw_output)

    save_json(RAW_OUTPUT_PATH, raw_output)
    save_json(STATE_PATH, state_output)
    save_json(DISPLAY_OUTPUT_PATH, display_output)

    print(f"Saved: {RAW_OUTPUT_PATH}", flush=True)
    print(f"Saved: {STATE_PATH}", flush=True)
    print(f"Saved: {DISPLAY_OUTPUT_PATH}", flush=True)
    print(f"Latest episodes: {latest_episode_numbers}", flush=True)
    print(f"Changed: {changed}", flush=True)


if __name__ == "__main__":
    main()
