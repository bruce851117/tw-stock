import json
import os
import time
import gzip
import zlib
import http.cookiejar
from datetime import datetime, timezone, timedelta
from urllib.request import Request, build_opener, HTTPCookieProcessor
from urllib.error import HTTPError, URLError


CONCEPT_ID = "C50919"
CONCEPT_NAME = "ASIC"

SOURCE_URL = f"https://www.cmoney.tw/forum/concept/{CONCEPT_ID}"
BASE_API_URL = f"https://www.cmoney.tw/api/mach/api/Article/StockCategory/{CONCEPT_ID}/Hottest"

OUTPUT_PATH = "data/test_asic_articles.json"
ERROR_PATH = "data/test_asic_error.json"

TAIPEI_TZ = timezone(timedelta(hours=8))


def now_taipei_string():
    return datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S")


def build_browser_headers(referer=None, accept_json=False, api_version_header=None):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }

    if accept_json:
        headers["Accept"] = "application/json, text/plain, */*"
        headers["Content-Type"] = "application/json;charset=UTF-8"
    else:
        headers["Accept"] = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        )

    if referer:
        headers["Referer"] = referer

    if api_version_header:
        header_name, header_value = api_version_header
        headers[header_name] = header_value

    return headers


def decode_response_body(response, raw_bytes):
    encoding = response.headers.get("Content-Encoding", "").lower()

    if encoding == "gzip":
        return gzip.decompress(raw_bytes).decode("utf-8", errors="replace")

    if encoding == "deflate":
        return zlib.decompress(raw_bytes).decode("utf-8", errors="replace")

    return raw_bytes.decode("utf-8", errors="replace")


def request_text(opener, url, headers):
    req = Request(url, headers=headers, method="GET")

    with opener.open(req, timeout=30) as response:
        raw = response.read()
        return decode_response_body(response, raw)


def try_fetch_api(opener):
    api_urls = [
        f"{BASE_API_URL}?fetch=10&startWeight=0",
        f"{BASE_API_URL}?api-version=2.0&fetch=10&startWeight=0",
        f"{BASE_API_URL}?fetch=10&startWeight=0&api-version=2.0",
        f"{BASE_API_URL}?api-version=1.0&fetch=10&startWeight=0",
        f"{BASE_API_URL}?fetch=10&startWeight=0&api-version=1.0",
    ]

    version_headers = [
        None,
        ("api-version", "2.0"),
        ("Api-Version", "2.0"),
        ("x-api-version", "2.0"),
        ("X-Api-Version", "2.0"),
        ("api-version", "1.0"),
        ("x-api-version", "1.0"),
    ]

    errors = []

    for api_url in api_urls:
        for version_header in version_headers:
            try:
                print("Trying API:")
                print(f"  url = {api_url}")
                print(f"  version_header = {version_header}")

                text = request_text(
                    opener,
                    api_url,
                    build_browser_headers(
                        referer=SOURCE_URL,
                        accept_json=True,
                        api_version_header=version_header,
                    ),
                )

                data = json.loads(text)

                if isinstance(data, dict) and "articles" in data:
                    print("API success")
                    return api_url, version_header, data

                errors.append({
                    "url": api_url,
                    "version_header": version_header,
                    "error": "Response JSON does not contain articles",
                    "body": text[:500],
                })

            except HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    body = ""

                errors.append({
                    "url": api_url,
                    "version_header": version_header,
                    "error": f"HTTP {e.code}",
                    "body": body[:500],
                })

            except Exception as e:
                errors.append({
                    "url": api_url,
                    "version_header": version_header,
                    "error": repr(e),
                    "body": "",
                })

            time.sleep(0.2)

    raise RuntimeError(json.dumps(errors, ensure_ascii=False, indent=2))


def fetch_json_with_session():
    cookie_jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))

    print(f"Open source page first: {SOURCE_URL}")

    try:
        request_text(
            opener,
            SOURCE_URL,
            build_browser_headers(
                referer="https://www.cmoney.tw/",
                accept_json=False,
            ),
        )
    except Exception as e:
        print(f"Warning: source page request failed, still try API. Error: {e}")

    time.sleep(1)

    return try_fetch_api(opener)


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


def save_error(message):
    os.makedirs("data", exist_ok=True)

    output = {
        "concept_id": CONCEPT_ID,
        "concept_name": CONCEPT_NAME,
        "source_url": SOURCE_URL,
        "base_api_url": BASE_API_URL,
        "fetched_at": now_taipei_string(),
        "success": False,
        "message": message,
    }

    with open(ERROR_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Saved error file: {ERROR_PATH}")


def main():
    os.makedirs("data", exist_ok=True)

    try:
        success_api_url, success_version_header, data = fetch_json_with_session()
    except Exception as e:
        message = str(e)
        save_error(message)
        raise RuntimeError(message) from e

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
        "api_url": success_api_url,
        "api_version_header": success_version_header,
        "fetched_at": now_taipei_string(),
        "success": True,
        "article_count": len(articles),
        "has_next": data.get("hasNext", False),
        "next_start_weight": data.get("nextStartWeight"),
        "articles": articles,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Saved: {OUTPUT_PATH}")
    print(f"Articles: {len(articles)}")
    print(f"Success API URL: {success_api_url}")
    print(f"Success version header: {success_version_header}")


if __name__ == "__main__":
    main()
