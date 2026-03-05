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


def _strip_bullet_prefix(text: str) -> str:
    """
    Strip leading bullet character + newline from text.

    Some PDF blocks embed the bullet marker ('l', 'n', '•') at the
    start of the text followed by a newline.  This helper removes
    it so the actual text can be translated cleanly.
    """
    stripped = text.strip()
    if len(stripped) >= 2 and stripped[0] in ('l', 'n', '\u2022', '\u25cf') and stripped[1] == '\n':
        return stripped[2:]
    return text


def _has_bullet_prefix(text: str) -> bool:
    """
    Return True if the text starts with an embedded bullet marker
    ('l', 'n', '•', '●') followed by a newline.

    Blocks with embedded bullet prefixes have their bounding box
    left edge at the bullet position — not at the readable text.
    Also returns True for standalone bullet markers (≤ 2 chars).
    """
    stripped = text.strip()
    if len(stripped) <= 2:
        return True
    if stripped[0] in ('l', 'n', '\u2022', '\u25cf') and stripped[1] == '\n':
        return True
    return False


def _merge_pointer_groups(blocks: list[dict]) -> list[list[int]]:
    """
    Detect groups of consecutive subheadline blocks that form a single
    bullet point / pointer.  These are short text fragments the PDF
    parser split across multiple lines (e.g. "7 अक्टूबर का हमला",
    "बर्बर, हमास के आतंक", "को कभी नहीं भूलेंगे" = one bullet).

    Returns a list of groups, each group is a list of block indices
    that should be merged before translation.  Single-block groups
    are omitted (no merge needed).

    Heuristic:
      - Only subheadline blocks with the same bg_color
      - Same column (left_pct within 5%)
      - Vertically adjacent (gap < 2% page height)
      - Skip bullet markers (single 'l' or ≤2 chars)
      - A bullet marker at the SAME vertical position as a text block
        signals the START of a new pointer (break the previous group)
    """
    groups: list[list[int]] = []
    current_group: list[int] = []

    # First pass: find which y-positions have bullet markers
    # (this helps detect where new pointers begin)
    # A bullet marker can be either:
    #   1. A standalone block with text ≤ 2 chars (e.g. just 'l')
    #   2. A block whose text starts with a bullet char + newline (e.g. "l\ntext")
    bullet_tops: set[float] = set()
    for blk in blocks:
        txt = blk.get("text", "").strip()
        role = blk.get("role", "body")
        if role != "subheadline":
            continue
        # Standalone bullet marker
        if len(txt) <= 2:
            bullet_tops.add(round(blk["top_pct"], 1))
        # Embedded bullet: text starts with single char + newline
        elif len(txt) > 2 and txt[0] in ('l', 'n', '\u2022', '\u25cf') and txt[1] == '\n':
            bullet_tops.add(round(blk["top_pct"], 1))

    def _is_at_bullet_position(blk: dict) -> bool:
        """Check if this block's top_pct aligns with a bullet marker."""
        return any(abs(round(blk["top_pct"], 1) - bt) < 0.5 for bt in bullet_tops)

    for i, blk in enumerate(blocks):
        role = blk.get("role", "body")
        text = blk.get("text", "").strip()
        clean_text = _strip_bullet_prefix(text).strip()

        # Skip standalone bullet markers (≤ 2 chars after stripping)
        if len(text) <= 2:
            continue

        # Only merge subheadline blocks
        if role != "subheadline":
            if len(current_group) > 1:
                groups.append(current_group)
            current_group = []
            continue

        # Skip blocks with no real text content (only bullet)
        if not clean_text:
            continue

        if not current_group:
            current_group = [i]
            continue

        # Check adjacency with last block in group
        last_blk = blocks[current_group[-1]]
        last_bottom = last_blk["top_pct"] + last_blk["height_pct"]
        gap = blk["top_pct"] - last_bottom
        same_col = abs(blk["left_pct"] - last_blk["left_pct"]) < 5.0
        same_bg = (blk.get("bg_color", "") == last_blk.get("bg_color", ""))
        # Same row: blocks at the same vertical position
        same_row = abs(blk["top_pct"] - last_blk["top_pct"]) < 0.5

        # If this block is at a bullet position AND not same row as
        # previous, it starts a new pointer
        is_new_pointer = _is_at_bullet_position(blk) and not same_row

        if same_bg and (same_row or (same_col and gap < 2.0)) and not is_new_pointer:
            current_group.append(i)
        else:
            if len(current_group) > 1:
                groups.append(current_group)
            current_group = [i]

    if len(current_group) > 1:
        groups.append(current_group)

    return groups


def _translate_blocks_batched(
    client: OpenAI | None,
    blocks: list[dict],
) -> None:
    """
    Translate an article's blocks, splitting into batches of
    MAX_BLOCKS_PER_BATCH to avoid LLM token overflow / key dropping.

    Before translation, consecutive subheadline fragments (pointers /
    bullet points) are merged into a single string so the LLM sees the
    full sentence and produces a coherent translation.  After
    translation, the merged English text is distributed back to each
    original block.

    Modifies blocks in-place, setting text_en on each block.
    """
    if client is None:
        for blk in blocks:
            blk["text_en"] = blk.get("text", "")
        return

    # ── Pre-merge pointer groups ──
    pointer_groups = _merge_pointer_groups(blocks)
    # Map: block index -> group info
    # merged_index_map[i] = (group_key, position_in_group)
    merged_index_map: dict[int, tuple[str, int, int]] = {}
    group_id = 0
    for grp in pointer_groups:
        gkey = f"pointer_group_{group_id}"
        for pos, idx in enumerate(grp):
            merged_index_map[idx] = (gkey, pos, len(grp))
        group_id += 1

    # Build the full keyed mapping
    role_counters: dict[str, int] = {}
    block_keys: list[str] = []
    for i, blk in enumerate(blocks):
        if i in merged_index_map:
            gkey, pos, glen = merged_index_map[i]
            if pos == 0:
                # First block in group: use group key
                key = gkey
            else:
                # Other blocks in group: will get their text from the group
                key = f"_skip_{i}"
        else:
            role = blk.get("role", "body")
            idx = role_counters.get(role, 0)
            role_counters[role] = idx + 1
            key = f"{role}_{idx}"
        block_keys.append(key)

    # Build keyed dict, merging pointer group texts
    full_keyed: dict[str, str] = {}
    for grp in pointer_groups:
        gkey = block_keys[grp[0]]
        merged_text = " ".join(
            _strip_bullet_prefix(blocks[idx].get("text", "")).strip()
            for idx in grp
            if _strip_bullet_prefix(blocks[idx].get("text", "")).strip()
        )
        full_keyed[gkey] = merged_text

    for i, blk in enumerate(blocks):
        key = block_keys[i]
        if key.startswith("_skip_"):
            continue
        if key.startswith("pointer_group_"):
            continue  # already added above
        text = blk.get("text", "").strip()
        # Strip leading bullet char from any block sent to translation
        text = _strip_bullet_prefix(text).strip()
        full_keyed[key] = text

    keys_list = [k for k in full_keyed.keys()]
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

    # Map back to blocks, distributing merged pointer translations
    for i, blk in enumerate(blocks):
        key = block_keys[i]
        if i in merged_index_map:
            gkey, pos, glen = merged_index_map[i]
            merged_en = translated.get(gkey, "")
            if pos == 0:
                # First block in pointer group: gets the full merged translation
                blk["text_en"] = merged_en
                # Mark this block to expand its bounding box in the renderer
                # to cover all blocks in this group
                grp_indices = [j for j, info in merged_index_map.items()
                               if info[0] == gkey]
                bottom_pct = max(
                    blocks[j]["top_pct"] + blocks[j]["height_pct"]
                    for j in grp_indices
                )

                # Compute left/right edges, EXCLUDING bullet-embedded blocks.
                # Blocks whose text starts with a bullet char + newline have
                # their left edge at the bullet position — using that would
                # make the overlay cover the bullet marker in the page image.
                text_only_indices = [
                    j for j in grp_indices
                    if not _has_bullet_prefix(blocks[j].get("text", ""))
                ]
                if not text_only_indices:
                    # All blocks have embedded bullets — fall back to leader
                    text_only_indices = [grp_indices[0]]

                left_pct = min(blocks[j]["left_pct"] for j in text_only_indices)
                right_pct = max(
                    blocks[j]["left_pct"] + blocks[j]["width_pct"]
                    for j in text_only_indices
                )
                blk["_pointer_bottom_pct"] = bottom_pct
                blk["_pointer_left_pct"] = left_pct
                blk["_pointer_width_pct"] = right_pct - left_pct
            else:
                # Subsequent blocks: mark as merged (renderer will skip)
                blk["text_en"] = ""
                blk["_pointer_merged"] = True
        else:
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
