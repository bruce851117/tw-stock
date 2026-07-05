"""
CMoney 概念股討論區爬蟲（多概念通用版）

讀取 data/concepts.json，逐一概念抓取 CMoney 討論區 Hottest API，
去重、正規化後存到 data/raw/<concept_id>.json。

由 test_cmoney_one.py 通用化而來，抓取邏輯（攔截 API + startWeight 翻頁）保持一致。
"""

import asyncio
import json
import os
from datetime import datetime, timezone, timedelta

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


CONCEPTS_PATH = "data/concepts.json"
RAW_DIR = "data/raw"

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


def load_concepts():
    with open(CONCEPTS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def build_headers_from_first_request(first_request_headers, source_url):
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
    useful_headers["referer"] = source_url
    useful_headers["x-version"] = useful_headers.get("x-version", "2.0")

    return useful_headers


async def capture_first_page(page, source_url, api_keyword):
    def is_target_api(response):
        return api_keyword in response.url and response.status == 200

    async with page.expect_response(is_target_api, timeout=60000) as response_info:
        await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

    response = await response_info.value
    data = await response.json()
    request_headers = await response.request.all_headers()

    return response.url, data, request_headers


async def fetch_page_by_context(context, api_url, headers):
    response = await context.request.get(api_url, headers=headers, timeout=60000)

    if not response.ok:
        body = await response.text()
        raise RuntimeError(
            f"API failed. status={response.status}, url={api_url}, body={body[:500]}"
        )

    return await response.json()


async def crawl_one_concept(context, concept):
    concept_id = concept["concept_id"]
    concept_name = concept.get("name", concept_id)

    source_url = f"https://www.cmoney.tw/forum/concept/{concept_id}"
    api_base_url = f"https://www.cmoney.tw/api/mach/api/Article/StockCategory/{concept_id}/Hottest"
    api_keyword = f"/api/mach/api/Article/StockCategory/{concept_id}/Hottest"

    print(f"\n=== Crawl {concept_name} ({concept_id}) ===", flush=True)
    print(f"Source URL: {source_url}", flush=True)

    page = await context.new_page()

    try:
        first_api_url, first_data, first_request_headers = await capture_first_page(
            page, source_url, api_keyword
        )
    finally:
        await page.close()

    headers = build_headers_from_first_request(first_request_headers, source_url)

    all_articles_raw = []
    seen_ids = set()

    def add_articles(page_no, data):
        raw_articles = data.get("articles") or []
        added = 0

        for article in raw_articles:
            article_id = str(article.get("id", "")).strip()
            if not article_id or article_id in seen_ids:
                continue
            seen_ids.add(article_id)
            all_articles_raw.append(article)
            added += 1

        print(
            f"Page {page_no}: raw={len(raw_articles)}, added={added}, "
            f"hasNext={data.get('hasNext')}, nextStartWeight={data.get('nextStartWeight')}",
            flush=True,
        )
        return data.get("hasNext", False), data.get("nextStartWeight")

    has_next, next_start_weight = add_articles(1, first_data)

    for page_no in range(2, MAX_PAGES + 1):
        if not has_next or not next_start_weight:
            break

        api_url = f"{api_base_url}?fetch={FETCH_COUNT}&startWeight={next_start_weight}"
        data = await fetch_page_by_context(context, api_url, headers)
        has_next, next_start_weight = add_articles(page_no, data)
        await asyncio.sleep(0.3)

    normalized = [normalize_article(a) for a in all_articles_raw]

    output = {
        "concept_id": concept_id,
        "concept_name": concept_name,
        "category": concept.get("category", ""),
        "note": concept.get("note", ""),
        "source_url": source_url,
        "fetched_at": now_taipei_string(),
        "article_count": len(normalized),
        "articles": normalized,
    }

    save_json(os.path.join(RAW_DIR, f"{concept_id}.json"), output)
    print(f"Saved: {RAW_DIR}/{concept_id}.json  ({len(normalized)} articles)", flush=True)


async def main_async():
    concepts = load_concepts()
    print(f"Loaded {len(concepts)} concept(s) from {CONCEPTS_PATH}", flush=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
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

        for concept in concepts:
            try:
                await crawl_one_concept(context, concept)
            except PlaywrightTimeoutError as e:
                print(f"[TIMEOUT] {concept.get('concept_id')}: {e}", flush=True)
            except Exception as e:
                print(f"[ERROR] {concept.get('concept_id')}: {e}", flush=True)

        await browser.close()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
