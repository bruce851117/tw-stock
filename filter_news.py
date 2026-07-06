"""
台股概念股新聞過濾 / 整理層

流程：
- 讀 data/concepts.json 與 data/raw/<concept_id>.json
- 本機預過濾：只留最近 TIME_WINDOW_HOURS 小時內的文章
- 分批送 Gemini：剔除廢文，保留有完整分析邏輯或產業/公司消息的文章
- Gemini 必須讀完整內文後摘要
- 摘要格式：先指出股票名稱與代號，再說明文章中的重點邏輯，以及對該公司的受益/不利影響
- 輸出 data/concept_news.json 給 index.html 顯示

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

# 可調參數
TIME_WINDOW_HOURS = 48
BATCH_SIZE = 15
SLEEP_BETWEEN_CALLS = 6.0

# 這裡提高到 12000，確保 Gemini 盡量讀完整內文。
# 只有極端長文才截斷，避免 prompt 爆掉。
TEXT_MAX_LEN = 12000

SUMMARY_TARGET_CHARS = 90
SUMMARY_MAX_CHARS = 110

MAX_RETRIES = 5
RETRY_BASE_DELAY = 4.0
RATELIMIT_DELAY = 30.0
RETRYABLE_STATUS = {429, 500, 503}

GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)


PROMPT_INSTRUCTION = """
你是台股概念股討論區的新聞篩選助理。

以下是同一個概念股討論區的多篇貼文，JSON 陣列中：
- id 為原文 id
- title 為原文標題
- text 為完整內文
- stocks 為原文標記到的股票代號
- concept_name 為概念股名稱
- concept_note 為此概念與產業鏈的說明

你的任務是：
1. 閱讀每篇貼文的完整內文 text。
2. 嚴格判斷是否值得保留。
3. 對保留的文章產生一段最多 80～100 個中文字左右的繁體中文摘要。

【保留條件】
只保留符合以下條件之一，且能連到明確公司或明確產業鏈受益/受害邏輯的貼文：

A. 有完整分析邏輯：
- 有前因後果
- 有推理、數據、供應鏈關係、客戶關係、產品規格、產能、報價、財報、法人動作等支撐
- 不是只有喊漲、喊跌、加油、看多、看空

B. 有產業面或公司面實質消息：
- 產業趨勢
- 供應鏈動態
- 公司營運、財報、接單、產能、產品、客戶、法說、法人動作
- 政策法規
- 國際大廠動態
- AI、半導體、伺服器、封裝、記憶體、電力、散熱、PCB 等產業鏈實質資訊

【必須剔除】
以下類型一律剔除，不要出現在結果中：

1. 純心情抒發、抱怨、問候、閒聊、貼圖、迷因。
2. 投資心法、心得、勵志雞湯、人生感悟。
3. 單純喊單、報明牌，沒有解釋為什麼。
4. 沒有分析的籌碼流水帳。
5. 廣告、拉群、導流、招收會員。
6. 單純大盤或市場價格描述，例如：
   - 台股本週強勢反彈
   - 加權指數上漲幾點
   - 外資賣超、內資承接
   - 傳產類股領漲、電子股量縮整理
   - 類股輪動撐盤
   這種若沒有進一步連到明確公司、產業供需、營運或獲利邏輯，必須剔除。
7. 只有市場氣氛、指數漲跌、類股輪動，沒有說明哪家公司因何受益或受害者，必須剔除。

【摘要格式要求】
對於保留文章，summary 必須符合以下格式：
- 請以原文有提到的內容作整理，而不是你自己推論的，或是你的想法
- 開頭先寫「股票名稱（代號）：」
- 接著摘要該股票在這篇文章中的重點。
- 必須包含「因為...所以...」的邏輯，但如果原文沒有的話就不用，不要加入原文沒有的推測，不要自行腦補!!!!!!!：
  例如因為某產品需求增加、某客戶拉貨、某規格升級、某產業趨勢、某公司供應鏈地位，因此這家公司可能受益；
  或因為價格下跌、需求轉弱、成本上升、競爭加劇，因此對這家公司不利。
- 若文章涉及多檔股票，請選出文章中最核心、最直接受影響的2~3檔股票來摘要。
- 摘要目標 80～100 個中文字，若內容不夠可以少字數沒關係，不要硬湊字數，最多不可超過 200 個中文字。
- 摘要必須是完整句子，必須以句號結尾。
- 不可以用「...」或「……」結尾。
- 不可以寫到一半中斷。


【輸出格式】
只回傳 JSON array。
每個元素格式如下：

{
  "id": "<原文 id>",
  "stock_name": "<股票名稱，若可判斷>",
  "stock_code": "<股票代號，若可判斷>",
  "summary": "<80～100字摘要，完整句子，以句號結尾>"
}

被剔除的貼文不要出現在結果中。
除了 JSON array，不要輸出任何其他文字。
"""


def now_taipei():
    return datetime.now(TAIPEI_TZ)


def now_taipei_string():
    return now_taipei().strftime("%Y-%m-%d %H:%M:%S")


def parse_article_time(time_str):
    """
    把 '2026-07-03 14:25' 解析成帶台北時區的 datetime。
    失敗回 None。
    """
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
    """
    本機預過濾：只留近 TIME_WINDOW_HOURS 小時內的文章。
    無法解析時間的先保留，交給 Gemini 判斷。
    """
    cutoff = now_taipei() - timedelta(hours=TIME_WINDOW_HOURS)
    kept = []

    for article in articles:
        dt = parse_article_time(article.get("time"))

        if dt is None or dt >= cutoff:
            kept.append(article)

    return kept


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def extract_json_array(text):
    """
    Gemini 有時會包 markdown 或多輸出文字。
    這裡只抽出第一個 JSON array。
    """
    text = str(text or "").strip()
    text = text.replace("```json", "").replace("```", "").strip()

    start = text.find("[")
    end = text.rfind("]")

    if start == -1 or end == -1 or end <= start:
        raise ValueError("Gemini response does not contain JSON array")

    return json.loads(text[start:end + 1])


def trim_summary_to_complete_sentence(summary):
    """
    保險：若 Gemini 回傳過長，回退到最近標點。
    不使用 ...，避免網頁看起來像被截斷。
    """
    summary = str(summary or "").strip()
    summary = summary.replace("...", "")
    summary = summary.replace("……", "")
    summary = " ".join(summary.split())

    if not summary:
        return ""

    if len(summary) <= SUMMARY_MAX_CHARS:
        if not summary.endswith(("。", "！", "？")):
            summary += "。"
        return summary

    # 優先找 70～110 字之間最後一個標點
    punctuation = ["。", "！", "？", "；"]
    best_pos = -1

    upper = min(len(summary), SUMMARY_MAX_CHARS)

    for i in range(0, upper):
        if summary[i] in punctuation:
            best_pos = i

    if best_pos >= 60:
        trimmed = summary[:best_pos + 1].strip()
        return trimmed

    # 如果沒有合適標點，最多截到上限並補句號
    trimmed = summary[:SUMMARY_MAX_CHARS].strip()
    trimmed = trimmed.rstrip("，、；：,. ")

    if not trimmed.endswith(("。", "！", "？")):
        trimmed += "。"

    return trimmed


def is_retryable_http_error(error):
    if isinstance(error, urllib.error.HTTPError):
        return error.code in RETRYABLE_STATUS

    return False


def post_gemini_with_retry(api_key, body):
    """
    對 Gemini 發 POST。
    遇到 429 / 500 / 503 或連線錯誤時退避重試。
    """
    url = f"{GEMINI_ENDPOINT}?key={api_key}"

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)

        except urllib.error.HTTPError as e:
            last_error = e
            body_text = ""

            try:
                body_text = e.read().decode("utf-8")[:1000]
            except Exception:
                body_text = ""

            print(
                f"[Gemini HTTPError] attempt={attempt}/{MAX_RETRIES}, "
                f"status={e.code}, body={body_text}",
                flush=True,
            )

            if e.code == 429:
                sleep_seconds = RATELIMIT_DELAY * attempt
            elif e.code in RETRYABLE_STATUS:
                sleep_seconds = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            else:
                raise

        except Exception as e:
            last_error = e
            print(
                f"[Gemini Error] attempt={attempt}/{MAX_RETRIES}, error={repr(e)}",
                flush=True,
            )
            sleep_seconds = RETRY_BASE_DELAY * (2 ** (attempt - 1))

        if attempt < MAX_RETRIES:
            print(f"Sleep {sleep_seconds:.1f}s before retry...", flush=True)
            time.sleep(sleep_seconds)

    raise RuntimeError(f"Gemini request failed after retries: {repr(last_error)}")


def parse_gemini_text_response(response_json):
    """
    從 Gemini generateContent response 取出文字。
    """
    candidates = response_json.get("candidates") or []

    if not candidates:
        raise ValueError("Gemini response has no candidates")

    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []

    if not parts:
        raise ValueError("Gemini response has no parts")

    text_parts = []

    for part in parts:
        if "text" in part:
            text_parts.append(part["text"])

    text = "\n".join(text_parts).strip()

    if not text:
        raise ValueError("Gemini response text is empty")

    return text


def call_gemini(api_key, payload_articles):
    """
    送一批文章給 Gemini，回傳：
    [
      {
        "id": "...",
        "stock_name": "...",
        "stock_code": "...",
        "summary": "..."
      }
    ]
    """
    compact = []

    for article in payload_articles:
        full_text = article.get("text", "") or ""

        compact.append({
            "id": article.get("id"),
            "title": article.get("title", ""),
            "stocks": article.get("stocks", []),
            "time": article.get("time", ""),
            "full_text": full_text[:TEXT_MAX_LEN],
        })

    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": PROMPT_INSTRUCTION + "\n\n貼文資料：\n" + json.dumps(compact, ensure_ascii=False)
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    response_json = post_gemini_with_retry(api_key, body)
    response_text = parse_gemini_text_response(response_json)
    parsed = extract_json_array(response_text)

    if not isinstance(parsed, list):
        raise ValueError("Gemini parsed result is not a list")

    cleaned = []

    for item in parsed:
        if not isinstance(item, dict):
            continue

        article_id = str(item.get("id", "")).strip()
        summary = trim_summary_to_complete_sentence(item.get("summary", ""))

        if not article_id or not summary:
            continue

        cleaned.append({
            "id": article_id,
            "stock_name": str(item.get("stock_name", "") or "").strip(),
            "stock_code": str(item.get("stock_code", "") or "").strip(),
            "summary": summary,
        })

    return cleaned


def filter_concept(api_key, raw):
    concept_id = raw.get("concept_id")
    concept_name = raw.get("concept_name", concept_id)
    category = raw.get("category", "")
    note = raw.get("note", "")
    source_url = raw.get("source_url", "")
    articles = raw.get("articles", [])

    recent_articles = prefilter_recent(articles)

    print(
        f"\n=== Filter {concept_name} ({concept_id}) === "
        f"raw={len(articles)}, recent={len(recent_articles)}",
        flush=True,
    )

    if not recent_articles:
        return {
            "concept_id": concept_id,
            "concept_name": concept_name,
            "category": category,
            "note": note,
            "source_url": source_url,
            "raw_count": len(articles),
            "recent_count": 0,
            "kept_count": 0,
            "news": [],
        }

    article_by_id = {
        str(article.get("id", "")).strip(): article
        for article in recent_articles
        if str(article.get("id", "")).strip()
    }

    kept_news = []

    for batch_no, batch in enumerate(chunked(recent_articles, BATCH_SIZE), start=1):
        print(
            f"Gemini batch {batch_no}: {concept_name}, articles={len(batch)}",
            flush=True,
        )

        # 把概念背景補進每篇文章，讓 Gemini 知道該概念的產業定位
        payload_batch = []

        for article in batch:
            cloned = dict(article)
            cloned["concept_name"] = concept_name
            cloned["concept_note"] = note
            payload_batch.append(cloned)

        try:
            gemini_results = call_gemini(api_key, payload_batch)
        except Exception as e:
            print(f"[ERROR] Gemini failed for {concept_name} batch {batch_no}: {e}", flush=True)
            gemini_results = []

        for result in gemini_results:
            article_id = str(result.get("id", "")).strip()
            original = article_by_id.get(article_id)

            if not original:
                continue

            summary = result.get("summary", "")

            news_item = {
                "id": article_id,
                "title": original.get("title", ""),
                "summary": summary,
                "stock_name": result.get("stock_name", ""),
                "stock_code": result.get("stock_code", ""),
                "time": original.get("time", ""),
                "stocks": original.get("stocks", []),
                "comment_count": original.get("comment_count", 0),
                "like_count": original.get("like_count", 0),
                "collected_count": original.get("collected_count", 0),
                "url": original.get("url", ""),
            }

            kept_news.append(news_item)

        time.sleep(SLEEP_BETWEEN_CALLS)

    # 去重
    seen_ids = set()
    unique_news = []

    for item in kept_news:
        if item["id"] in seen_ids:
            continue

        seen_ids.add(item["id"])
        unique_news.append(item)

    unique_news.sort(key=lambda x: x.get("time", ""), reverse=True)

    print(
        f"Kept {len(unique_news)} / recent {len(recent_articles)} for {concept_name}",
        flush=True,
    )

    return {
        "concept_id": concept_id,
        "concept_name": concept_name,
        "category": category,
        "note": note,
        "source_url": source_url,
        "raw_count": len(articles),
        "recent_count": len(recent_articles),
        "kept_count": len(unique_news),
        "news": unique_news,
    }


def main():
    api_key = os.environ.get("GEMINI_API_KEY")

    if not api_key:
        raise SystemExit("缺少環境變數 GEMINI_API_KEY")

    concepts = load_json(CONCEPTS_PATH)
    output_concepts = []

    # 全域文章去重：
    # 同一篇 article id 如果出現在多個 concept，
    # 只保留在 concepts.json 順序中第一個出現的 concept。
    global_seen_article_ids = set()

    for concept in concepts:
        concept_id = concept.get("concept_id")
        concept_name = concept.get("name", concept_id)
        raw_path = f"{RAW_DIR}/{concept_id}.json"

        if not os.path.exists(raw_path):
            print(f"[WARN] Raw file not found: {raw_path}", flush=True)
            output_concepts.append({
                "concept_id": concept_id,
                "concept_name": concept_name,
                "category": concept.get("category", ""),
                "note": concept.get("note", ""),
                "source_url": concept.get("url", ""),
                "raw_count": 0,
                "recent_count": 0,
                "kept_count": 0,
                "dedup_removed_count": 0,
                "news": [],
            })
            continue

        raw = load_json(raw_path)

        # 若 raw 裡缺欄位，用 concepts.json 補
        raw["concept_name"] = raw.get("concept_name") or concept_name
        raw["category"] = raw.get("category") or concept.get("category", "")
        raw["note"] = raw.get("note") or concept.get("note", "")
        raw["source_url"] = raw.get("source_url") or concept.get("url", "")

        result = filter_concept(api_key, raw)

        original_news = result.get("news", [])
        deduped_news = []
        dedup_removed_count = 0

        for item in original_news:
            article_id = str(item.get("id", "")).strip()

            if not article_id:
                deduped_news.append(item)
                continue

            if article_id in global_seen_article_ids:
                dedup_removed_count += 1
                continue

            global_seen_article_ids.add(article_id)
            deduped_news.append(item)

        if dedup_removed_count > 0:
            print(
                f"Global dedupe removed {dedup_removed_count} duplicate article(s) from {concept_name}",
                flush=True,
            )

        result["news"] = deduped_news
        result["kept_count_before_global_dedupe"] = result.get("kept_count", len(original_news))
        result["dedup_removed_count"] = dedup_removed_count
        result["kept_count"] = len(deduped_news)

        output_concepts.append(result)

    output = {
        "updated_at": now_taipei_string(),
        "timezone": "Asia/Taipei",
        "time_window_hours": TIME_WINDOW_HOURS,
        "model": GEMINI_MODEL,
        "concept_count": len(output_concepts),
        "global_unique_article_count": len(global_seen_article_ids),
        "concepts": output_concepts,
    }

    save_json(OUTPUT_PATH, output)

    total_recent = sum(c.get("recent_count", 0) for c in output_concepts)
    total_kept_before_global_dedupe = sum(
        c.get("kept_count_before_global_dedupe", c.get("kept_count", 0))
        for c in output_concepts
    )
    total_dedup_removed = sum(c.get("dedup_removed_count", 0) for c in output_concepts)
    total_kept = sum(c.get("kept_count", 0) for c in output_concepts)

    print("\n=== Done ===", flush=True)
    print(f"Concepts: {len(output_concepts)}", flush=True)
    print(f"Recent articles: {total_recent}", flush=True)
    print(f"Kept before global dedupe: {total_kept_before_global_dedupe}", flush=True)
    print(f"Global dedupe removed: {total_dedup_removed}", flush=True)
    print(f"Final kept articles: {total_kept}", flush=True)
    print(f"Global unique article ids: {len(global_seen_article_ids)}", flush=True)
    print(f"Saved: {OUTPUT_PATH}", flush=True)

if __name__ == "__main__":
    main()