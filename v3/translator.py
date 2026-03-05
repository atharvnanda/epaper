"""
v3 translator -- role-aware block-level Hindi -> English translation.

Reads  data/{date}/pdf_blocks.json   (output of v3/pdf_parser.py)
Writes data/{date}/articles_translated.json

Strategy (Approach B):
  For each article, build a keyed dict like:
    { "headline_0": "...", "body_0": "...", "body_1": "...", ... }
  Send to LLM with a structured prompt asking it to return JSON with
  the same keys translated. Then map each translation back to its
  original block by key.

This ensures:
  - Headlines stay headline-length (short, punchy)
  - Body text stays body-length
  - No mixing between blocks
  - 1:1 mapping for rendering
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

DATA_DIR = "data"

SYSTEM_PROMPT = """\
You are a professional Hindi-to-English newspaper translator.

You will receive a JSON object where each key is a labeled text block
from a Hindi newspaper article (e.g. "headline_0", "body_0", "body_1").

Translate every value into polished, editorially fluent English.

CRITICAL RULES:
- Return ONLY a valid JSON object with the EXACT same keys.
- You MUST translate EVERY key. Do NOT leave any value empty.
- You MUST NOT return any Hindi/Devanagari text. Translate everything.
- Even if a value is a single word or sentence fragment, translate it.
- headline translations should be concise and impactful (newspaper headline style).
- subheadline translations should be brief (1-2 lines).
- IMPORTANT: Some subheadline blocks may be FRAGMENTS of a single bullet point
  split across multiple lines. Each fragment should still be translated as a
  grammatically correct phrase or sentence, NOT as disconnected words.
  For example: "बर्बर, हमास के आतंक" should translate to "Barbaric; Hamas' terror"
  not just "Brutal, Hamas terrorists".
- body translations should be natural English paragraphs.
- byline: just translate the location/author, keep it short.
- caption: brief descriptive text.
- Keep proper nouns, names, places as-is (Modi, Netanyahu, Ambani, etc.).
- "आजतक" = "Aaj Tak".
- Do NOT add any explanation, markdown, or extra text outside the JSON.
"""

# Articles shorter than this total chars are kept untranslated
MIN_TRANSLATE_CHARS = 15

# Max blocks per LLM call -- prevents token overflow and dropped keys
MAX_BLOCKS_PER_BATCH = 12


def _make_client() -> OpenAI | None:
    """Create Groq-compatible OpenAI client, or None if no API key."""
    load_dotenv()
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        print("  WARN: GROQ_API_KEY not set -- text_en will copy text_hi.")
        return None
    return OpenAI(
        api_key=api_key,
        base_url="https://api.groq.com/openai/v1",
    )


def _build_keyed_dict(blocks: list[dict]) -> dict[str, str]:
    """
    Build a role-indexed dict from an article's blocks.

    Example output:
      {"headline_0": "...", "subheadline_0": "...",
       "body_0": "...", "body_1": "...", "byline_0": "..."}
    """
    role_counters: dict[str, int] = {}
    keyed: dict[str, str] = {}

    for blk in blocks:
        role = blk.get("role", "body")
        idx = role_counters.get(role, 0)
        role_counters[role] = idx + 1
        key = f"{role}_{idx}"
        keyed[key] = blk.get("text", "").strip()

    return keyed


def _parse_json_response(raw: str) -> dict[str, str] | None:
    """Try to parse a JSON dict from the LLM response, tolerating markdown fences."""
    text = raw.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        # Remove opening fence (```json or ```)
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        # Remove closing fence
        text = re.sub(r"\n?```\s*$", "", text)
        text = text.strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in the text
    match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    return None


def _translate_article_keyed(
    client: OpenAI | None,
    keyed_dict: dict[str, str],
    retries: int = 3,
) -> dict[str, str]:
    """
    Translate a keyed dict of blocks via LLM.
    Returns a dict with same keys but English values.
    Falls back to Hindi text on failure.
    """
    if client is None:
        return keyed_dict  # fallback

    user_msg = json.dumps(keyed_dict, ensure_ascii=False, indent=2)

    for attempt in range(1, retries + 1):
        try:
            resp = client.chat.completions.create(
                model="meta-llama/llama-4-maverick-17b-128e-instruct",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.15,
                max_tokens=4096,
            )
            raw = (resp.choices[0].message.content or "").strip()
            parsed = _parse_json_response(raw)

            if parsed is not None:
                # Ensure all keys exist; reject empty or still-Hindi values
                result = {}
                for k in keyed_dict:
                    val = (parsed.get(k) or "").strip()
                    if val and not _is_still_hindi(val):
                        result[k] = val
                    else:
                        # LLM returned empty or Hindi -- keep Hindi as fallback
                        result[k] = keyed_dict[k]
                return result
            else:
                print(f"    WARN: could not parse JSON from LLM (attempt {attempt})")
                if attempt == retries:
                    return keyed_dict

        except Exception as exc:
            print(f"    translate error (attempt {attempt}): {exc}")
            if attempt < retries:
                time.sleep(1.5 * attempt)

    return keyed_dict  # fallback after all retries


# Hindi / Devanagari detection
_HINDI_RE = re.compile(r"[\u0900-\u097F]")


def _is_still_hindi(text: str) -> bool:
    """Return True if more than 30% of the text is Devanagari characters."""
    if not text:
        return False
    hindi_chars = len(_HINDI_RE.findall(text))
    alpha_chars = sum(1 for c in text if c.isalpha())
    if alpha_chars == 0:
        return False
    return hindi_chars / alpha_chars > 0.3


def _translate_blocks_batched(
    client: OpenAI | None,
    blocks: list[dict],
) -> None:
    """
    Translate an article's blocks, splitting into batches of
    MAX_BLOCKS_PER_BATCH to avoid LLM token overflow / key dropping.

    Modifies blocks in-place, setting text_en on each block.
    """
    if client is None:
        for blk in blocks:
            blk["text_en"] = blk.get("text", "")
        return

    # Build the full keyed mapping first
    role_counters: dict[str, int] = {}
    block_keys: list[str] = []
    for blk in blocks:
        role = blk.get("role", "body")
        idx = role_counters.get(role, 0)
        role_counters[role] = idx + 1
        key = f"{role}_{idx}"
        block_keys.append(key)

    # Split into batches
    full_keyed = {block_keys[i]: blocks[i].get("text", "").strip()
                  for i in range(len(blocks))}

    keys_list = list(full_keyed.keys())
    translated = {}

    for batch_start in range(0, len(keys_list), MAX_BLOCKS_PER_BATCH):
        batch_keys = keys_list[batch_start:batch_start + MAX_BLOCKS_PER_BATCH]
        batch_dict = {k: full_keyed[k] for k in batch_keys}

        batch_result = _translate_article_keyed(client, batch_dict)
        translated.update(batch_result)

        # Rate limit between batches
        if batch_start + MAX_BLOCKS_PER_BATCH < len(keys_list):
            time.sleep(0.3)

    # Retry any blocks that are still Hindi
    retry_dict = {}
    for k in keys_list:
        val = translated.get(k, "")
        if _is_still_hindi(val):
            retry_dict[k] = full_keyed[k]

    if retry_dict:
        print(f"    Retrying {len(retry_dict)} still-Hindi blocks...")
        retry_result = _translate_article_keyed(client, retry_dict)
        for k, v in retry_result.items():
            if not _is_still_hindi(v):
                translated[k] = v

    # Map back to blocks
    for i, blk in enumerate(blocks):
        key = block_keys[i]
        blk["text_en"] = translated.get(key, blk.get("text", ""))


def translate_articles(date_str: str) -> dict[str, Any]:
    """
    Read pdf_blocks.json, translate each article block-by-block,
    write articles_translated.json.

    Each block gets its own text_en field (1:1 mapping).
    """
    in_json = os.path.join(DATA_DIR, date_str, "pdf_blocks.json")
    out_json = os.path.join(DATA_DIR, date_str, "articles_translated.json")

    if not os.path.exists(in_json):
        raise FileNotFoundError(f"Input not found: {in_json}")

    with open(in_json, "r", encoding="utf-8") as f:
        src = json.load(f)

    client = _make_client()

    pages_out: list[dict[str, Any]] = []
    translated_count = 0
    skipped_count = 0

    for page in src.get("pages", []):
        articles_out: list[dict[str, Any]] = []

        for article in page.get("articles", []):
            blocks = article.get("blocks", [])
            total_text = "".join(b.get("text", "") for b in blocks)

            if len(total_text.strip()) < MIN_TRANSLATE_CHARS:
                # Short article -- keep Hindi text as-is per block
                for blk in blocks:
                    blk["text_en"] = blk.get("text", "")
                skipped_count += 1
            else:
                # Translate with batching for large articles
                _translate_blocks_batched(client, blocks)
                translated_count += 1

            articles_out.append({
                "article_id": article["article_id"],
                "source": article.get("source", ""),
                "top_pct": article["top_pct"],
                "left_pct": article["left_pct"],
                "width_pct": article["width_pct"],
                "height_pct": article["height_pct"],
                "block_count": article["block_count"],
                "text": article.get("text", ""),
                "blocks": blocks,
            })

        pages_out.append({
            "page_num": page["page_num"],
            "page_w": page["page_w"],
            "page_h": page["page_h"],
            "image": page["image"],
            "articles": articles_out,
        })

    result = {
        "date": date_str,
        "source": str(Path(in_json).resolve()),
        "pages": pages_out,
    }

    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"  Translated {translated_count} articles, skipped {skipped_count}")
    print(f"  Saved: {out_json}")
    return result


if __name__ == "__main__":
    import sys
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-03-02"
    translate_articles(date)
