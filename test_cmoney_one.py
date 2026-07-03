import asyncio
import json
import os
from datetime import datetime, timezone, timedelta

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


CONCEPT_ID = "C50919"
CONCEPT_NAME = "ASIC"

SOURCE_URL = f"https://www.cmoney.tw/forum/concept/{CONCEPT_ID}"
API_BASE_URL = f"https://www.cmoney.tw/api/mach/api/Article/StockCategory/{CONCEPT_ID}/Hottest"
API_KEYWORD = f"/api/mach/api/Article/StockCategory/{CONCEPT_ID}/Hottest"

OUTPUT_PATH = "data/test_asic_articles.json"
ERROR_PATH = "data/test_asic_error.json"

MAX_PAGES = 100
FETCH_COUNT = 10

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


def make_summary(text, max_len=160):
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
        "money_count": emoji_count.get("money", 0),
        "shock_count": emoji_count.get("shock", 0),
        "think_count": emoji_count.get("think", 0),
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


def build_api_url(start_weight):
    return f"{API_BASE_URL}?fetch={FETCH_COUNT}&startWeight={start_weight}"


def build_headers_from_first_request(first_request_headers):
    useful_headers = {}

    for key in [
        "accept",
        "accept-language",
        "authorization",
        "cmoneyapi-trace-context",
        "referer",
        "user-agent",
        "x-version",
    ]:
        value = first_request_headers.get(key)
        if value:
            useful_headers[key] = value

    useful_headers["accept"] = "application/json, text/plain, */*"
    useful_headers["referer"] = SOURCE_URL
    useful_headers["x-version"] = useful_headers.get("x-version", "2.0")

    return useful_headers


async def capture_first_page(page):
    print("Capture first Hottest API by browser", flush=True)

    def is_target_api(response):
        return API_KEYWORD in response.url and response.status == 200

    async with page.expect_response(is_target_api, timeout=60000) as response_info:
        await page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=60000)

    response = await response_info.value
    data = await response.json()
    request_headers = await response.request.all_headers()

    print(f"Captured first API: {response.url}", flush=True)

    return response.url, data, request_headers


async def fetch_page_by_context(context, api_url, headers):
    response = await context.request.get(api_url, headers=headers, timeout=60000)

    if not response.ok:
        body = await response.text()
        raise RuntimeError(
            f"API failed. status={response.status}, url={api_url}, body={body[:500]}"
        )

    return await response.json()


async def crawl_100_pages():
    print("main_async started", flush=True)
    print(f"Source URL: {SOURCE_URL}", flush=True)
    print(f"Max pages: {MAX_PAGES}", flush=True)

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

        first_api_url, first_data, first_request_headers = await capture_first_page(page)

        headers = build_headers_from_first_request(first_request_headers)

        all_articles_raw = []
        page_summaries = []

        seen_ids = set()

        def add_articles(page_no, api_url, data):
            raw_articles = data.get("articles") or []
            added_count = 0

            for article in raw_articles:
                article_id = str(article.get("id", "")).strip()

                if not article_id:
                    continue

                if article_id in seen_ids:
                    continue

                seen_ids.add(article_id)
                all_articles_raw.append(article)
                added_count += 1

            page_summaries.append({
                "page": page_no,
                "api_url": api_url,
                "raw_count": len(raw_articles),
                "added_count": added_count,
                "has_next": data.get("hasNext", False),
                "next_start_weight": data.get("nextStartWeight"),
            })

            print(
                f"Page {page_no}: raw={len(raw_articles)}, added={added_count}, "
                f"hasNext={data.get('hasNext')}, nextStartWeight={data.get('nextStartWeight')}",
                flush=True,
            )

        add_articles(1, first_api_url, first_data)

        has_next = first_data.get("hasNext", False)
        next_start_weight = first_data.get("nextStartWeight")

        for page_no in range(2, MAX_PAGES + 1):
            if not has_next:
                print("No more pages. Stop.", flush=True)
                break

            if not next_start_weight:
                print("No nextStartWeight. Stop.", flush=True)
                break

            api_url = build_api_url(next_start_weight)

            data = await fetch_page_by_context(context, api_url, headers)

            add_articles(page_no, api_url, data)

            has_next = data.get("hasNext", False)
            next_start_weight = data.get("nextStartWeight")

            await page.wait_for_timeout(300)

        await browser.close()

        return page_summaries, all_articles_raw


async def main_async():
    os.makedirs("data", exist_ok=True)

    try:
        page_summaries, all_articles_raw = await crawl_100_pages()
    except PlaywrightTimeoutError as e:
        save_error("PlaywrightTimeoutError", str(e))
        raise
    except Exception as e:
        save_error("Exception", str(e))
        raise

    normalized_articles = [normalize_article(article) for article in all_articles_raw]

    output = {
        "concept_id": CONCEPT_ID,
        "concept_name": CONCEPT_NAME,
        "source_url": SOURCE_URL,
        "fetched_at": now_taipei_string(),
        "success": True,
        "max_pages": MAX_PAGES,
        "fetched_pages": len(page_summaries),
        "article_count": len(normalized_articles),
        "page_summaries": page_summaries,
        "articles": normalized_articles,
    }

    save_json(OUTPUT_PATH, output)

    print(f"Saved: {OUTPUT_PATH}", flush=True)
    print(f"Fetched pages: {len(page_summaries)}", flush=True)
    print(f"Unique articles: {len(normalized_articles)}", flush=True)


def main():
    print("main started", flush=True)
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
