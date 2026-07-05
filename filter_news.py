"""
新聞過濾 / 整理層

流程：
  1. 讀 data/concepts.json 與 data/raw/<concept_id>.json
  2. 本機預過濾：只留最近 TIME_WINDOW_HOURS 小時內的文章（省 token、避開每日上限）
  3. 分批送 Gemini 2.5 Flash-Lite：剔除廢文，保留重要文章並產生「一句話重點」
  4. 輸出 data/concept_news.json 給 index.html 顯示

Gemini 只做「留/丟 + 一句重點」，不做評分。
需要環境變數 GEMINI_API_KEY。
"""

import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta


CONCEPTS_PATH = "data/concepts.json"
RAW_DIR = "data/raw"
OUTPUT_PATH = "data/concept_news.json"

TAIPEI_TZ = timezone(timedelta(hours=8))

# --- 可調參數 ---
TIME_WINDOW_HOURS = 48       # 只處理近 N 小時的文章
BATCH_SIZE = 18              # 每次送 Gemini 的文章數
SLEEP_BETWEEN_CALLS = 4.0    # 批次間隔秒數（避開免費版 RPM 上限）
TEXT_EXCERPT_LEN = 200       # 送給 Gemini 的內文擷取長度
MAX_RETRIES = 4              # 暫時性錯誤（429/500/503）重試次數
RETRY_BASE_DELAY = 3.0       # 重試退避基準秒數（指數成長）

# 這些 HTTP 狀態碼視為暫時性、值得重試（過載 / 限流 / 伺服器暫時錯誤）
RETRYABLE_STATUS = {429, 500, 503}

GEMINI_MODEL = "gemini-2.5-flash-lite"
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

PROMPT_INSTRUCTION = """你是台股討論區的新聞篩選助理。以下是同一個概念股討論區的多篇貼文（JSON 陣列）。

請幫我「剔除廢文、只保留有資訊價值的貼文」。

判斷標準：
- 保留：具體事件、財報/營收、法人或大戶動作、產業趨勢、公司新聞、有數據或有論述的分析。
- 剔除：純心情抒發、問候閒聊、貼圖、單純喊單/報明牌、無內容的洗版、重複資訊、廣告或拉群。

對於「保留」的每一篇，請產生一句 30 字內的中文重點摘要（point），聚焦這篇在講什麼事。

只回傳一個 JSON 陣列，每個元素格式為：
{"id": "<原文 id>", "point": "<一句話重點>"}
被剔除的貼文請不要出現在結果中。除了 JSON 陣列外不要輸出任何其他文字。"""


def now_taipei():
    return datetime.now(TAIPEI_TZ)


def parse_article_time(time_str):
    """把 '2026-07-03 14:25' 解析成帶台北時區的 datetime；失敗回 None。"""
    if not time_str:
        return None
    try:
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=TAIPEI_TZ)
    except ValueError:
        return None


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def prefilter_recent(articles):
    """本機預過濾：只留近 TIME_WINDOW_HOURS 小時內的文章。"""
    cutoff = now_taipei() - timedelta(hours=TIME_WINDOW_HOURS)
    kept = []
    for a in articles:
        dt = parse_article_time(a.get("time"))
        if dt is None or dt >= cutoff:
            # 無法解析時間的先保留，交給 Gemini 判斷
            kept.append(a)
    return kept


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def post_gemini_with_retry(api_key, body):
    """對 Gemini 發 POST，遇到暫時性錯誤（429/500/503/連線錯誤）會退避重試。"""
    req = urllib.request.Request(
        f"{GEMINI_ENDPOINT}?key={api_key}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                print(
                    f"[RETRY] Gemini HTTP {e.code}，第 {attempt}/{MAX_RETRIES} 次，"
                    f"{delay:.0f}s 後重試",
                    flush=True,
                )
                time.sleep(delay)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                print(
                    f"[RETRY] Gemini 連線錯誤（{e}），第 {attempt}/{MAX_RETRIES} 次，"
                    f"{delay:.0f}s 後重試",
                    flush=True,
                )
                time.sleep(delay)
                continue
            raise

    if last_err:
        raise last_err


def call_gemini(api_key, payload_articles):
    """送一批文章給 Gemini，回傳 [{id, point}, ...]。"""
    compact = [
        {
            "id": a.get("id"),
            "title": a.get("title", ""),
            "text": (a.get("text", "") or "")[:TEXT_EXCERPT_LEN],
        }
        for a in payload_articles
    ]

    prompt = PROMPT_INSTRUCTION + "\n\n貼文資料：\n" + json.dumps(
        compact, ensure_ascii=False
    )

    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }

    result = post_gemini_with_retry(api_key, body)

    try:
        text = result["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        print(f"[WARN] Unexpected Gemini response: {json.dumps(result)[:500]}", flush=True)
        return []

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        print(f"[WARN] Gemini did not return valid JSON: {text[:300]}", flush=True)
        return []

    if not isinstance(parsed, list):
        return []
    return parsed


def filter_concept(api_key, raw):
    concept_id = raw.get("concept_id")
    concept_name = raw.get("concept_name", concept_id)
    articles = raw.get("articles", [])

    recent = prefilter_recent(articles)
    print(
        f"[{concept_name}] 原始 {len(articles)} → 近{TIME_WINDOW_HOURS}h {len(recent)} 篇",
        flush=True,
    )

    by_id = {str(a.get("id")): a for a in recent}
    kept_points = {}  # id -> point

    for batch_no, batch in enumerate(chunked(recent, BATCH_SIZE), start=1):
        print(f"[{concept_name}] Gemini batch {batch_no}（{len(batch)} 篇）", flush=True)
        try:
            results = call_gemini(api_key, batch)
        except urllib.error.HTTPError as e:
            print(f"[ERROR] Gemini HTTP {e.code}: {e.read().decode('utf-8')[:300]}", flush=True)
            results = []
        except Exception as e:
            print(f"[ERROR] Gemini call failed: {e}", flush=True)
            results = []

        for item in results:
            aid = str(item.get("id", "")).strip()
            point = str(item.get("point", "")).strip()
            if aid in by_id and point:
                kept_points[aid] = point

        time.sleep(SLEEP_BETWEEN_CALLS)

    # 依原始順序組出留下來的新聞
    news = []
    for a in recent:
        aid = str(a.get("id"))
        if aid in kept_points:
            news.append({
                "time": a.get("time", ""),
                "title": a.get("title", ""),
                "point": kept_points[aid],
                "stocks": a.get("stocks", []),
                "url": a.get("url", ""),
            })

    print(f"[{concept_name}] 保留 {len(news)} 篇重要新聞", flush=True)

    return {
        "concept_id": concept_id,
        "concept_name": concept_name,
        "category": raw.get("category", ""),
        "note": raw.get("note", ""),
        "source_url": raw.get("source_url", ""),
        "news": news,
    }


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("缺少環境變數 GEMINI_API_KEY")

    concepts = load_json(CONCEPTS_PATH)
    output_concepts = []

    for concept in concepts:
        concept_id = concept["concept_id"]
        raw_path = os.path.join(RAW_DIR, f"{concept_id}.json")
        if not os.path.exists(raw_path):
            print(f"[SKIP] 找不到 {raw_path}，請先執行 crawl_cmoney.py", flush=True)
            continue

        raw = load_json(raw_path)
        output_concepts.append(filter_concept(api_key, raw))

    output = {
        "updated_at": now_taipei().strftime("%Y-%m-%d %H:%M:%S"),
        "time_window_hours": TIME_WINDOW_HOURS,
        "model": GEMINI_MODEL,
        "concepts": output_concepts,
    }

    save_json(OUTPUT_PATH, output)
    print(f"\nSaved: {OUTPUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
