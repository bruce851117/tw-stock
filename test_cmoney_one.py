import asyncio
import json
import os
from datetime import datetime, timezone, timedelta

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


CONCEPT_ID = "C50919"
CONCEPT_NAME = "ASIC"

SOURCE_URL = f"https://www.cmoney.tw/forum/concept/{CONCEPT_ID}"
API_KEYWORD = f"/api/mach/api/Article/StockCategory/{CONCEPT_ID}/Hottest"

OUTPUT_PATH = "data/test_asic_articles.json"
ERROR_PATH = "data/test_asic_error.json"

TAIPEI_TZ = timezone(timedelta(hours=8))


def now_taipei_string():
    return datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S")


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


def make_summary(text, max_len=120):
    text = clean_text(text)
    one_line = " ".join(text.split())

    if len(one_line) <= max_len:
        return one_line

    return one_line[:max_len] + "..."


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
        "summary": make_summary(text),
        "time": timestamp_ms_to_taipei(article.get("createTime")),
        "modify_time": timestamp_ms_to_taipei(article.get("modifyTime")),
        "stocks": stocks,
        "comment_count": article.get("commentCount", 0),
        "like_count": emoji_count.get("like", 0),
        "laugh_count": emoji_count.get("laugh", 0),
        "collected_count": article.get("collectedCount", 0),
        "url": f"https://www.cmoney.tw/forum/article/{article_id}" if article_id else "",
    }


def save_json(path, data):
    os.makedirs("data", exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_error(error_type, message):
    output = {
        "concept_id": CONCEPT_ID,
        "concept_name": CONCEPT_NAME,
        "source_url": SOURCE_URL,
        "fetched_at": now_taipei_string(),
        "success": False,
        "error_type": error_type,
        "message": message,
    }

    save_json(ERROR_PATH, output)
    print(f"Saved error file: {ERROR_PATH}", flush=True)


async def fetch_cmoney_api_by_browser():
    print("Start Playwright crawler", flush=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = await browser.new_context(
            locale="zh-TW",
            timezone_id="Asia/Taipei",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/146.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 1200},
        )

        page = await context.new_page()

        print(f"Open page: {SOURCE_URL}", flush=True)

        def is_target_api(response):
            return API_KEYWORD in response.url and response.status == 200

        try:
            async with page.expect_response(is_target_api, timeout=60000) as response_info:
                await page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=60000)

            response = await response_info.value
            api_url = response.url
            data = await response.json()

            print(f"Captured API: {api_url}", flush=True)

            await browser.close()
            return api_url, data

        except PlaywrightTimeoutError:
            await browser.close()
            raise RuntimeError(
                "Timeout: No Hottest API response captured. "
                "CMoney page may require login, block headless browser, or API timing changed."
            )


async def main_async():
    print("main_async started", flush=True)

    os.makedirs("data", exist_ok=True)

    try:
        api_url, data = await fetch_cmoney_api_by_browser()
    except Exception as e:
        save_error("PlaywrightError", str(e))
        raise

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
        "api_url": api_url,
        "fetched_at": now_taipei_string(),
        "success": True,
        "article_count": len(articles),
        "has_next": data.get("hasNext", False),
        "next_start_weight": data.get("nextStartWeight"),
        "articles": articles,
    }

    save_json(OUTPUT_PATH, output)

    print(f"Saved: {OUTPUT_PATH}", flush=True)
    print(f"Articles: {len(articles)}", flush=True)


def main():
    print("main started", flush=True)
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
