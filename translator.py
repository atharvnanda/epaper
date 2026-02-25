"""
Step 2: Translate articles from Hindi to English using Grok API (xAI).
"""

import json
import os
import time

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

DATA_DIR = "data"
RAW_FILE = os.path.join(DATA_DIR, "articles_raw.json")
TRANSLATED_FILE = os.path.join(DATA_DIR, "articles_translated.json")

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

Respond in this exact JSON format:
{{
  "headline_en": "...",
  "body_en": "..."
}}"""


def translate_articles():
    """Load raw articles, translate via Grok, save translated JSON."""

    api_key = os.getenv("GROK_API_KEY")
    if not api_key or api_key == "your_grok_api_key_here":
        print("  ERROR: Set GROK_API_KEY in .env file")
        return

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.x.ai/v1",
    )

    # Load raw data
    if not os.path.exists(RAW_FILE):
        print(f"  ERROR: {RAW_FILE} not found. Run scraper.py first.")
        return

    with open(RAW_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Check if we have a partial translation to resume from
    existing_translations = {}
    if os.path.exists(TRANSLATED_FILE):
        with open(TRANSLATED_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)
        # Build cache of already-translated storyids
        for pg in existing.get("pages", []):
            for art in pg.get("articles", []):
                if art.get("headline_en"):
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

    # Translate each unique article
    translated_cache = {}
    for i, (sid, art) in enumerate(to_translate.items(), 1):
        headline_hi = art.get("headline_hi", "")
        body_hi = art.get("body_hi", "")

        if not headline_hi and not body_hi:
            print(f"    [{i}/{len(to_translate)}] storyid={sid} — no Hindi text, skipping")
            continue

        print(f"    [{i}/{len(to_translate)}] Translating storyid={sid}...")
        print(f"      Hindi headline: {headline_hi[:60]}...")

        try:
            prompt = TRANSLATION_PROMPT.format(
                hindi_headline=headline_hi,
                hindi_body=body_hi or "(no body text)",
            )

            response = client.chat.completions.create(
                model="grok-3-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )

            reply = response.choices[0].message.content.strip()

            # Parse JSON from response (handle markdown code blocks)
            if "```json" in reply:
                reply = reply.split("```json")[1].split("```")[0].strip()
            elif "```" in reply:
                reply = reply.split("```")[1].split("```")[0].strip()

            result = json.loads(reply)
            headline_en = result.get("headline_en", "")
            body_en = result.get("body_en", "")

            translated_cache[sid] = {
                "headline_en": headline_en,
                "body_en": body_en,
            }
            print(f"      ✓ English: {headline_en[:60]}...")

        except json.JSONDecodeError:
            print(f"      ⚠ Could not parse JSON from API response. Keeping Hindi text.")
            translated_cache[sid] = {
                "headline_en": headline_hi,
                "body_en": body_hi,
            }
        except Exception as e:
            print(f"      ✗ API error: {e}. Keeping Hindi text.")
            translated_cache[sid] = {
                "headline_en": headline_hi,
                "body_en": body_hi,
            }

        time.sleep(1)  # Rate limiting

    # Apply translations to all article entries (including duplicates across pages)
    for pg in data["pages"]:
        for art in pg["articles"]:
            sid = art["storyid"]
            if sid in translated_cache:
                art["headline_en"] = translated_cache[sid]["headline_en"]
                art["body_en"] = translated_cache[sid]["body_en"]
            elif sid not in existing_translations:
                # No translation available — keep Hindi as fallback
                art["headline_en"] = art.get("headline_hi", "")
                art["body_en"] = art.get("body_hi", "")

    # Save
    with open(TRANSLATED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    total = sum(len(pg["articles"]) for pg in data["pages"])
    translated_count = sum(
        1 for pg in data["pages"]
        for a in pg["articles"]
        if a.get("headline_en") and a["headline_en"] != a.get("headline_hi")
    )
    print(f"\n  Done! Saved {TRANSLATED_FILE}")
    print(f"  Total article zones: {total}")
    print(f"  Newly translated: {len(translated_cache)}")
    print(f"  From cache: {len(existing_translations)}")


if __name__ == "__main__":
    translate_articles()
