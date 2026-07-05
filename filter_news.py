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
BATCH_SIZE = 15              # 每次送 Gemini 的文章數（批次越大請求越少，越省免費額度）
SLEEP_BETWEEN_CALLS = 6.0    # 批次間隔秒數（避開免費版每分鐘上限）
TEXT_MAX_LEN = 4000          # 送給 Gemini 的內文長度上限（幾乎等於完整內文，僅防極端長文）
SUMMARY_MAX_CHARS = 100      # 摘要硬上限（超過就截斷，保險用）
MAX_RETRIES = 5              # 暫時性錯誤（429/500/503）重試次數
RETRY_BASE_DELAY = 4.0       # 503/連線錯誤退避基準秒數（指數成長）
RATELIMIT_DELAY = 30.0       # 429（額度/速率）退避秒數（每分鐘額度約 60s 回補，乘以次數）

# 這些 HTTP 狀態碼視為暫時性、值得重試（過載 / 限流 / 伺服器暫時錯誤）
RETRYABLE_STATUS = {429, 500, 503}

GEMINI_MODEL = "gemini-2.5-flash-lite"
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

PROMPT_INSTRUCTION = """你是台股概念股討論區的新聞篩選助理。以下是同一個概念股討論區的多篇貼文（JSON 陣列，text 為完整內文）。

請嚴格篩選，「只保留」符合下列其中一種的貼文：
1. 有完整分析邏輯：有前因後果、有推理論述或數據支撐的分析（不是只喊「會漲/會跌」，而是有講為什麼）。
2. 產業面消息資訊：產業趨勢、供應鏈動態、公司營運/財報/法人動作、政策法規、國際大廠或客戶動態等實質資訊。

「必須剔除」以下類型（就算被概念標籤帶到也要剔除）：
- 純心情抒發、抱怨、問候閒聊、貼圖、迷因。
- 投資心法、心得、心路歷程、勵志雞湯、人生感悟（沒有針對特定公司或產業的具體分析）。
- 單純喊單、報明牌、無論述地說買/賣或目標價。
- 沒有分析的籌碼流水帳、單句評論、洗版、重複資訊。
- 廣告、拉群、招收會員、導流。

寧可嚴一點：如果一篇沒有針對特定公司/產業的實質分析、也沒有產業消息，就剔除。

對於「保留」的每一篇，請閱讀完整內文後，寫一段繁體中文摘要（summary），濃縮這篇的分析重點或產業資訊，讓讀者不用點進原文就能掌握重點。
摘要「務必嚴格控制在 100 個字以內」，超過 100 字視為不合格，請自行精簡。

只回傳一個 JSON 陣列，每個元素格式為：
{"id": "<原文 id>", "summary": "<100字內摘要>"}
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
                if e.code == 429:
                    # 額度/速率限制：等久一點讓每分鐘額度回補
                    delay = RATELIMIT_DELAY * attempt
                else:
                    # 503/500 過載：指數退避
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
    """送一批文章給 Gemini，回傳 [{id, summary}, ...]。"""
    compact = [
        {
            "id": a.get("id"),
            "title": a.get("title", ""),
            "text": (a.get("text", "") or "")[:TEXT_MAX_LEN],
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
    kept_summaries = {}  # id -> summary

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
            summary = str(item.get("summary", "")).strip()
            if aid in by_id and summary:
                # 保險：Gemini 偶爾超過 100 字，硬截斷
                if len(summary) > SUMMARY_MAX_CHARS:
                    summary = summary[:SUMMARY_MAX_CHARS].rstrip() + "…"
                kept_summaries[aid] = summary

        time.sleep(SLEEP_BETWEEN_CALLS)

    # 依原始順序組出留下來的新聞
    news = []
    for a in recent:
        aid = str(a.get("id"))
        if aid in kept_summaries:
            news.append({
                "time": a.get("time", ""),
                "title": a.get("title", ""),
                "summary": kept_summaries[aid],
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
