import json
import os
import time
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


CONCEPT_ID = "C50919"
CONCEPT_NAME = "ASIC"

API_URL = f"https://www.cmoney.tw/api/mach/api/Article/StockCategory/{CONCEPT_ID}/Hottest?fetch=10&startWeight=0"
SOURCE_URL = f"https://www.cmoney.tw/forum/concept/{CONCEPT_ID}"

OUTPUT_PATH = "data/test_asic_articles.json"

TAIPEI_TZ = timezone(timedelta(hours=8))


def fetch_json(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": SOURCE_URL,
        "Origin": "https://www.cmoney.tw",
    }

    req = Request(url, headers=headers, method="GET")

    with urlopen(req, timeout=20) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw)


def timestamp_ms_to_taipei(ms):
    if not ms:
        return ""

    try:
        seconds = int(ms) / 1000
        dt = datetime.fromtimestamp(seconds, tz=TAIPEI_TZ)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def clean_text(value):
    if not value:
        return ""

    return (
        str(value)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )


def make_title(title, text):
    title = clean_text(title)
    text = clean_text(text)

    if title:
        return title

    if text:
        one_line = " ".join(text.split())
        return one_line[:40] + ("..." if len(one_line) > 40 else "")

    return "未命名文章"


def normalize_article(article):
    content = article.get("content") or {}

    article_id = str(article.get("id", "")).strip()
    title = make_title(content.get("title"), content.get("text"))
    text = clean_text(content.get("text"))

    commodity_tags = content.get("commodityTags") or []
    stocks = []

    for tag in commodity_tags:
        if tag.get("type") == "Stock" and tag.get("key"):
            stocks.append(str(tag.get("key")))

    emoji_count = article.get("emojiCount") or {}

    return {
        "id": article_id,
        "title": title,
        "text": text,
        "summary": " ".join text.split()[:1] if False else text.replace("\n", " ")[:120],
        "time": timestamp_ms_to_taipei(article.get("createTime")),
        "modify_time": timestamp_ms_to_taipei(article.get("modifyTime")),
        "stocks": stocks,
        "comment_count": article.get("commentCount", 0),
        "like_count": emoji_count.get("like", 0),
        "laugh_count": emoji_count.get("laugh", 0),
        "collected_count": article.get("collectedCount", 0),
        "url": f"https://www.cmoney.tw/forum/article/{article_id}" if article_id else "",
    }


def main():
    os.makedirs("data", exist_ok=True)

    print(f"Fetching: {API_URL}")

    try:
        data = fetch_json(API_URL)
    except HTTPError as e:
        raise RuntimeError(f"HTTP error: {e.code}") from e
    except URLError as e:
        raise RuntimeError(f"URL error: {e}") from e

    raw_articles = data.get("articles") or []

    seen_ids = set()
    articles = []

    for article in raw_articles:
        article_id = str(article.get("id", "")).strip()

        if not article_id:
            continue

        if article_id in seen_ids:
            continue

        seen_ids.add(article_id)
        articles.append(normalize_article(article))

    output = {
        "concept_id": CONCEPT_ID,
        "concept_name": CONCEPT_NAME,
        "source_url": SOURCE_URL,
        "api_url": API_URL,
        "fetched_at": datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "article_count": len(articles),
        "has_next": data.get("hasNext", False),
        "next_start_weight": data.get("nextStartWeight"),
        "articles": articles,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Saved: {OUTPUT_PATH}")
    print(f"Articles: {len(articles)}")


if __name__ == "__main__":
    main()
