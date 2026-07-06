import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


TAIPEI_TZ = ZoneInfo("Asia/Taipei")

DATA_DIR = Path("data")
RAW_INPUT_PATH = DATA_DIR / "gooaye_latest_raw.json"
HISTORY_PATH = DATA_DIR / "gooaye_history.json"
DISPLAY_OUTPUT_PATH = DATA_DIR / "gooaye_news.json"

SOURCE_NAME = "股癌筆記"
SOURCE_URL = "https://socialworkerdaily.com/index/invest/notes-of-gooaye/"

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
MAX_CONTENT_CHARS = int(os.getenv("GOOAYE_MAX_CONTENT_CHARS", "60000"))
REQUEST_SLEEP_SECONDS = 1.0


def now_taipei_string():
    return datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S")


def read_json(path, default):
    if not path.exists():
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def clean_text(value):
    if value is None:
        return ""

    text = str(value)
    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")

    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")

    return text.strip()


def sha256_text(value):
    text = clean_text(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_json_object(text):
    if not text:
        raise ValueError("Empty Gemini response text")

    text = text.strip()

    if text.startswith("```json"):
        text = text[len("```json"):].strip()

    if text.startswith("```"):
        text = text[len("```"):].strip()

    if text.endswith("```"):
        text = text[:-len("```")].strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    decoder = json.JSONDecoder()

    positions = []
    for index, char in enumerate(text):
        if char == "{":
            positions.append(index)

    for start in positions:
        try:
            parsed, end = decoder.raw_decode(text[start:])
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    bad_path = DATA_DIR / "gooaye_bad_gemini_response.txt"

    with open(bad_path, "w", encoding="utf-8") as f:
        f.write(text)

    raise ValueError("No valid JSON object found in Gemini response. Saved to " + str(bad_path))


def build_prompt(episode_item):
    episode = episode_item.get("episode", "")
    title = episode_item.get("title", "")
    url = episode_item.get("url", "")
    published_at = episode_item.get("published_at", "")
    modified_at = episode_item.get("modified_at", "")
    content = clean_text(episode_item.get("content", ""))

    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS] + "\n\n[內容過長，已截斷]"

    prompt = f"""
你是一位專業台股、總體經濟、產業趨勢研究助理。請整理以下「股癌筆記」文章。
請你首先先保留 壹、節目內容 裡面的內容，刪除後面 貳、心得感想 參、操作日記 等等的內容，再來做以下步驟

請務必遵守：
1. 只輸出 JSON。
2. 不要輸出 Markdown。
3. 不要輸出任何 JSON 以外的說明文字。
4. 使用繁體中文。
5. 不要加入你自己的投資建議。
6. 如果原文有作者個人操作紀錄，請放在 personal_trade_note，不要視為推薦。
7. 如果提到股票，請盡量保留股票名稱與代號。
8. 如果沒有明確代號，code 請放空字串。

請輸出這個 JSON 結構：

{{
  "episode": {episode},
  "title": "",
  "published_at": "",
  "url": "",
  "summary": "約250到450字，整理本集最重要的台股與市場重點",
  "key_points": [
    "重點1",
    "重點2",
    "重點3"
  ],
  "market_topics": [
    {{
      "topic": "族群或主題名稱",
      "summary": "該主題重點",
      "sentiment": "positive"
    }}
  ],
  "mentioned_stocks": [
    {{
      "name": "股票名稱",
      "code": "股票代號或空字串",
      "reason": "原文提到的原因或題材"
    }}
  ],
  "risk_notes": [
    "風險或待觀察事項"
  ],
  "personal_trade_note": "若原文有作者個人操作紀錄則摘要，否則空字串"
}}

文章資訊：
episode: {episode}
title: {title}
url: {url}
published_at: {published_at}
modified_at: {modified_at}

文章全文：
{content}
""".strip()

    return prompt


def call_gemini(prompt):
    api_key = os.getenv("GEMINI_API_KEY", "").strip()

    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY")

    api_url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + GEMINI_MODEL
        + ":generateContent?key="
        + api_key
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "topP": 0.8,
            "topK": 40,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json"
        }
    }

    response = requests.post(api_url, json=payload, timeout=120)

    if not response.ok:
        raise RuntimeError(
            "Gemini API failed: status="
            + str(response.status_code)
            + ", body="
            + response.text[:1000]
        )

    data = response.json()

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as exc:
        short = json.dumps(data, ensure_ascii=False)[:1000]
        raise RuntimeError("Unexpected Gemini response: " + short) from exc

    return extract_json_object(text)


def list_or_empty(value):
    if isinstance(value, list):
        return value

    return []


def normalize_summary_result(result, episode_item):
    episode = episode_item.get("episode")
    title = episode_item.get("title", "")
    url = episode_item.get("url", "")
    published_at = episode_item.get("published_at", "")
    modified_at = episode_item.get("modified_at", "")
    content = clean_text(episode_item.get("content", ""))

    if not isinstance(result, dict):
        result = {}

    normalized = {
        "episode": int(result.get("episode") or episode),
        "title": clean_text(result.get("title") or title),
        "url": result.get("url") or url,
        "published_at": result.get("published_at") or published_at,
        "modified_at": modified_at,
        "summary": clean_text(result.get("summary", "")),
        "key_points": list_or_empty(result.get("key_points")),
        "market_topics": list_or_empty(result.get("market_topics")),
        "mentioned_stocks": list_or_empty(result.get("mentioned_stocks")),
        "risk_notes": list_or_empty(result.get("risk_notes")),
        "personal_trade_note": clean_text(result.get("personal_trade_note", "")),
        "content_hash": sha256_text(content),
        "content_length": len(content),
        "summarized_at": now_taipei_string(),
        "model": GEMINI_MODEL
    }

    return normalized


def make_fallback_summary(episode_item, error_message):
    episode = episode_item.get("episode")
    content = clean_text(episode_item.get("content", ""))
    fallback_summary = content[:1200]

    if len(content) > 1200:
        fallback_summary = fallback_summary + "..."

    return {
        "episode": int(episode),
        "title": episode_item.get("title", ""),
        "url": episode_item.get("url", ""),
        "published_at": episode_item.get("published_at", ""),
        "modified_at": episode_item.get("modified_at", ""),
        "summary": fallback_summary,
        "key_points": [],
        "market_topics": [],
        "mentioned_stocks": [],
        "risk_notes": [
            "本集 Gemini 整理失敗，暫時顯示原文前段。"
        ],
        "personal_trade_note": "",
        "content_hash": sha256_text(content),
        "content_length": len(content),
        "summarized_at": now_taipei_string(),
        "model": GEMINI_MODEL,
        "error": str(error_message)
    }


def needs_summarize(episode_item, history_episodes):
    episode = str(episode_item.get("episode"))
    content_hash = sha256_text(episode_item.get("content", ""))
    saved = history_episodes.get(episode)

    if not saved:
        return True, "missing_history"

    if not saved.get("summary"):
        return True, "missing_summary"

    if saved.get("content_hash") != content_hash:
        return True, "content_changed"

    return False, "cached"


def make_display_json(raw_data, history):
    history_episodes = history.get("episodes", {})
    articles = []

    for item in raw_data.get("episodes", []):
        episode_key = str(item.get("episode"))
        saved = history_episodes.get(episode_key, {})

        summary = saved.get("summary")

        if not summary:
            content = clean_text(item.get("content", ""))
            summary = content[:800]

            if len(content) > 800:
                summary = summary + "..."

        articles.append({
            "episode": item.get("episode"),
            "title": saved.get("title") or item.get("title"),
            "url": saved.get("url") or item.get("url"),
            "published_at": saved.get("published_at") or item.get("published_at"),
            "modified_at": saved.get("modified_at") or item.get("modified_at"),
            "time": saved.get("published_at") or item.get("published_at"),
            "summary": summary,
            "key_points": saved.get("key_points", []),
            "market_topics": saved.get("market_topics", []),
            "mentioned_stocks": saved.get("mentioned_stocks", []),
            "risk_notes": saved.get("risk_notes", []),
            "personal_trade_note": saved.get("personal_trade_note", ""),
            "model": saved.get("model", ""),
            "summarized_at": saved.get("summarized_at", "")
        })

    return {
        "source_name": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "latest_range_url": raw_data.get("latest_range_url", ""),
        "updated_at": now_taipei_string(),
        "article_count": len(articles),
        "articles": articles
    }


def ensure_history_shape(history):
    if not isinstance(history, dict):
        history = {}

    if "episodes" not in history:
        history["episodes"] = {}

    if not isinstance(history["episodes"], dict):
        history["episodes"] = {}

    if "source_name" not in history:
        history["source_name"] = SOURCE_NAME

    if "source_url" not in history:
        history["source_url"] = SOURCE_URL

    return history


def save_history_and_display(raw_data, history):
    history["source_name"] = SOURCE_NAME
    history["source_url"] = SOURCE_URL
    history["updated_at"] = now_taipei_string()
    history["latest_range_url"] = raw_data.get("latest_range_url", "")
    history["latest_episodes"] = raw_data.get("latest_episodes", [])
    history["model"] = GEMINI_MODEL

    display_output = make_display_json(raw_data, history)

    save_json(HISTORY_PATH, history)
    save_json(DISPLAY_OUTPUT_PATH, display_output)


def main():
    raw_data = read_json(RAW_INPUT_PATH, None)

    if not raw_data:
        raise RuntimeError("Missing raw input: " + str(RAW_INPUT_PATH))

    history = read_json(
        HISTORY_PATH,
        {
            "source_name": SOURCE_NAME,
            "source_url": SOURCE_URL,
            "updated_at": "",
            "episodes": {}
        }
    )

    history = ensure_history_shape(history)
    history_episodes = history["episodes"]

    raw_episodes = raw_data.get("episodes", [])

    if not raw_episodes:
        raise RuntimeError("No episodes found in gooaye raw data")

    print("==== Gooaye summarize check ====", flush=True)
    print("Latest episodes: " + str(raw_data.get("latest_episodes")), flush=True)
    print("Raw changed flag: " + str(raw_data.get("changed")), flush=True)
    print("Model: " + GEMINI_MODEL, flush=True)

    summarized_count = 0
    skipped_count = 0
    failed_count = 0

    for item in raw_episodes:
        episode = item.get("episode")
        need, reason = needs_summarize(item, history_episodes)

        if not need:
            print("EP" + str(episode) + ": skip Gemini (" + reason + ")", flush=True)
            skipped_count = skipped_count + 1
            continue

        print("EP" + str(episode) + ": summarize with Gemini (" + reason + ")", flush=True)

        try:
            prompt = build_prompt(item)
            result = call_gemini(prompt)
            normalized = normalize_summary_result(result, item)

            history_episodes[str(episode)] = normalized
            summarized_count = summarized_count + 1

            print("EP" + str(episode) + ": Gemini summarize success", flush=True)

        except Exception as exc:
            print("EP" + str(episode) + ": Gemini summarize failed: " + str(exc), flush=True)

            fallback = make_fallback_summary(item, exc)
            history_episodes[str(episode)] = fallback
            failed_count = failed_count + 1

        save_history_and_display(raw_data, history)

        time.sleep(REQUEST_SLEEP_SECONDS)

    save_history_and_display(raw_data, history)

    print("==== Gooaye summarize result ====", flush=True)
    print("Summarized: " + str(summarized_count), flush=True)
    print("Skipped: " + str(skipped_count), flush=True)
    print("Failed fallback: " + str(failed_count), flush=True)
    print("Saved: " + str(HISTORY_PATH), flush=True)
    print("Saved: " + str(DISPLAY_OUTPUT_PATH), flush=True)


if __name__ == "__main__":
    main()
