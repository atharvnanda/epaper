"""
Step 2: Translate articles from Hindi to English using Groq Cloud API.
"""

import json
import os
import re
import time

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

DATA_DIR = "data"

# Truncate very long article bodies to avoid exceeding model token limits.
# ~2000 Hindi characters ≈ 800-1000 tokens; keeps well within context window.
MAX_BODY_CHARS = 3000


def _paths(date_str: str):
    """Return date-namespaced file paths."""
    data_dir = os.path.join(DATA_DIR, date_str)
    raw_file = os.path.join(data_dir, "articles_raw.json")
    translated_file = os.path.join(data_dir, "articles_translated.json")
    return raw_file, translated_file


TRANSLATION_PROMPT = """You are a professional Hindi-to-English newspaper translator.
Translate the following Hindi newspaper article into polished, 
editorially fluent English that reads like a reputable English 
daily newspaper (like Times of India or The Hindu).

Rules:
- Keep proper nouns as-is (names, places, party names)
- Preserve the journalistic tone and urgency
- Do not add or remove information
- Paragraph breaks must be preserved

Translate the HEADLINE and BODY separately.

HEADLINE (Hindi): {hindi_headline}
BODY (Hindi): {hindi_body}

Respond ONLY with this JSON (no other text):
{{
  "headline_en": "...",
  "body_en": "..."
}}"""


MAX_RETRIES = 5          # max retries on 429 / transient errors
INITIAL_BACKOFF = 2      # seconds — doubles each retry


class DailyLimitExhausted(Exception):
    """Raised when the daily token quota is exhausted."""
    pass


def _parse_retry_seconds(error_str: str) -> int | None:
    """Extract 'try again in XmYs' from Groq error message → total seconds."""
    m = re.search(r'try again in (\d+)m([\d.]+)s', error_str, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 60 + int(float(m.group(2)))
    m = re.search(r'try again in ([\d.]+)s', error_str, re.IGNORECASE)
    if m:
        return int(float(m.group(1)))
    return None


# Maximum seconds we're willing to wait for a daily limit reset
MAX_DAILY_WAIT = 900  # 15 minutes


def _extract_json(text: str) -> dict | None:
    """Robustly extract a JSON object from potentially messy model output.

    Tries, in order:
      1. Direct json.loads on the whole string
      2. Strip markdown code fences (```json ... ``` or ``` ... ```
      3. Regex: find the first { ... } block that parses as JSON
      4. Regex: look for "headline_en" and "body_en" values and build dict
    Returns parsed dict or None.
    """
    if not text:
        return None

    # 1. Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Strip markdown code fences
    cleaned = text
    if "```json" in cleaned:
        cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in cleaned:
        cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 3. Regex: find the outermost { ... } containing our keys
    #    Use a greedy match for the last } to handle nested braces
    match = re.search(r'\{[^{}]*"headline_en"[^{}]*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Broader: first { to last }
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # 4. Last resort: extract values with regex
    hl_match = re.search(r'"headline_en"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.DOTALL)
    bd_match = re.search(r'"body_en"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.DOTALL)
    if hl_match:
        headline = hl_match.group(1).replace('\\"', '"')
        body = bd_match.group(1).replace('\\"', '"') if bd_match else ""
        return {"headline_en": headline, "body_en": body}

    return None


def _call_api_with_retry(client, prompt: str) -> str:
    """Call the Groq API. On any 429 / rate-limit error, just raise immediately."""
    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=4096,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        raise


def translate_articles(date_str: str):
    """Load raw articles, translate via Groq Cloud, save translated JSON."""

    raw_file, translated_file = _paths(date_str)

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key or api_key.startswith("your_"):
        print("  ERROR: Set GROQ_API_KEY in .env file")
        return

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.groq.com/openai/v1",
    )

    # Load raw data
    if not os.path.exists(raw_file):
        print(f"  ERROR: {raw_file} not found. Run scraper.py first.")
        return

    with open(raw_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Check if we have a partial translation to resume from
    existing_translations = {}
    if os.path.exists(translated_file):
        with open(translated_file, "r", encoding="utf-8") as f:
            existing = json.load(f)
        # Build cache of already-translated storyids
        # Only cache if the English text differs from Hindi (i.e. real translation)
        for pg in existing.get("pages", []):
            for art in pg.get("articles", []):
                if art.get("headline_en") and art["headline_en"] != art.get("headline_hi", ""):
                    existing_translations[art["storyid"]] = {
                        "headline_en": art["headline_en"],
                        "body_en": art["body_en"],
                    }
        print(f"  Found {len(existing_translations)} already-translated articles (cache)")

    # Collect unique articles that need translation
    to_translate = {}
    for pg in data["pages"]:
        for art in pg["articles"]:
            sid = art["storyid"]
            if sid in existing_translations:
                # Already translated — apply cached translation
                art["headline_en"] = existing_translations[sid]["headline_en"]
                art["body_en"] = existing_translations[sid]["body_en"]
            elif sid not in to_translate and art.get("headline_hi"):
                to_translate[sid] = art

    print(f"  Articles to translate: {len(to_translate)}")

    def _save_progress():
        """Apply translated_cache to data and write to disk."""
        for pg in data["pages"]:
            for art in pg["articles"]:
                sid = art["storyid"]
                if sid in translated_cache:
                    art["headline_en"] = translated_cache[sid]["headline_en"]
                    art["body_en"] = translated_cache[sid]["body_en"]
                elif sid not in existing_translations:
                    art["headline_en"] = art.get("headline_hi", "")
                    art["body_en"] = art.get("body_hi", "")
        with open(translated_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # Translate each unique article
    translated_cache = {}
    daily_limit_hit = False
    for i, (sid, art) in enumerate(to_translate.items(), 1):
        headline_hi = art.get("headline_hi", "")
        body_hi = art.get("body_hi", "")

        if not headline_hi and not body_hi:
            print(f"    [{i}/{len(to_translate)}] storyid={sid} — no Hindi text, skipping")
            continue

        # Truncate very long bodies to avoid token limit issues
        body_for_api = body_hi or "(no body text)"
        if len(body_for_api) > MAX_BODY_CHARS:
            body_for_api = body_for_api[:MAX_BODY_CHARS] + "..."
            print(f"    [{i}/{len(to_translate)}] Translating storyid={sid} (body truncated to {MAX_BODY_CHARS} chars)...")
        else:
            print(f"    [{i}/{len(to_translate)}] Translating storyid={sid}...")
        print(f"      Hindi headline: {headline_hi[:60]}...")

        try:
            prompt = TRANSLATION_PROMPT.format(
                hindi_headline=headline_hi,
                hindi_body=body_for_api,
            )

            reply = _call_api_with_retry(client, prompt)

            # Robust JSON extraction from model output
            result = _extract_json(reply)
            if result is None:
                print(f"      ⚠ Could not parse JSON from API response. Keeping Hindi text.")
                print(f"        Raw reply (first 200 chars): {reply[:200]}")
                translated_cache[sid] = {
                    "headline_en": headline_hi,
                    "body_en": body_hi,
                }
            else:
                headline_en = result.get("headline_en", "")
                body_en = result.get("body_en", "")

                translated_cache[sid] = {
                    "headline_en": headline_en,
                    "body_en": body_en,
                }
                print(f"      ✓ English: {headline_en[:60]}...")

            _save_progress()  # save after every article

        except DailyLimitExhausted as e:
            print(f"\n  ⚠ Daily token limit exhausted! Saving partial progress.")
            print(f"    Error detail: {str(e)[:300]}")
            print(f"    Re-run later to translate remaining articles.")
            translated_cache[sid] = {
                "headline_en": headline_hi,
                "body_en": body_hi,
            }
            daily_limit_hit = True
            break

        except Exception as e:
            print(f"      ✗ API error: {e}. Keeping Hindi text.")
            translated_cache[sid] = {
                "headline_en": headline_hi,
                "body_en": body_hi,
            }

        time.sleep(1)  # Rate limiting

    # Final save
    _save_progress()

    total = sum(len(pg["articles"]) for pg in data["pages"])
    translated_count = sum(
        1 for pg in data["pages"]
        for a in pg["articles"]
        if a.get("headline_en") and a["headline_en"] != a.get("headline_hi")
    )
    print(f"\n  Done! Saved {translated_file}")
    print(f"  Total article zones: {total}")
    print(f"  Newly translated: {len(translated_cache)}")
    print(f"  From cache: {len(existing_translations)}")
    print(f"  Total translated: {translated_count}/{total}")
    if daily_limit_hit:
        remaining = len(to_translate) - i
        print(f"  ⚠ {remaining} articles still need translation (daily limit hit)")
        print(f"    Re-run this script later when the quota resets.")


if __name__ == "__main__":
    import sys
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-02-25"
    translate_articles(date)
