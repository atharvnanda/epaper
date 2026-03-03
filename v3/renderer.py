"""
v3 renderer -- generate epaper HTML from translated articles.

Reads  data/{date}/articles_translated.json
Writes output/{date}/epaper.html

Each block is rendered as an absolutely-positioned overlay at its
exact PDF coordinates. Only text blocks get overlays -- images and
graphics stay fully visible underneath.

Block-level rendering means:
  - Each block's text_en goes into that block's exact bbox
  - Role-based styling (headline = bold large, body = serif small)
  - No distribution / splitting logic needed

Self-contained output:
  - Page images are embedded as base64 data URIs so the HTML file
    can be shared / opened on any machine without the images folder.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from jinja2 import Environment, FileSystemLoader

DATA_DIR = "data"
OUTPUT_DIR = "output"
TEMPLATE_DIR = "templates"


def _embed_image(image_path: str) -> str:
    """
    Read an image file and return a base64 data URI string.
    Falls back to the original relative path if the file is not found,
    so the non-embedded HTML still works when images are present locally.
    """
    if not os.path.exists(image_path):
        # Graceful fallback: keep relative path (works when opened locally)
        return image_path
    with open(image_path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _is_short_symbol(text: str) -> bool:
    """
    Return True if the text is just a bullet/symbol/single char
    that shouldn't be rendered as a full overlay.
    Catches: single 'l' (Wingdings bullets), quote marks, bullets, etc.
    """
    stripped = text.strip()
    if len(stripped) <= 2:
        return True
    # Also skip if it's ONLY quote characters (curly/straight quotes)
    quote_chars = set('"""\u201c\u201d\u2018\u2019\u201e\u201a«»\u2039\u203a\'')
    if all(c in quote_chars for c in stripped):
        return True
    return False


def _merge_body_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Merge body-role blocks that belong to the same column into single
    tall blocks with concatenated text.

    The PDF parser extracts each LINE as a separate block (~1.13% height).
    English text is often longer than Hindi, so one-line boxes overflow.
    By merging all body lines in the same column region, the text can
    reflow naturally within a larger block.

    Strategy:
      1. Separate body blocks from non-body blocks (preserve order).
      2. Group body blocks by column (using left_pct proximity).
      3. Within each column group, sort by top_pct and merge vertically
         adjacent blocks into one tall block.
      4. Reassemble all blocks sorted by top_pct.
    """
    if not blocks:
        return blocks

    # Separate body and non-body blocks
    non_body: list[dict[str, Any]] = []
    body_blocks: list[dict[str, Any]] = []
    for blk in blocks:
        if blk.get("role", "body") == "body":
            body_blocks.append(blk)
        else:
            non_body.append(blk)

    if not body_blocks:
        return blocks

    # Group body blocks by column using left_pct proximity
    # Two blocks are in the same column if their left edges are within 5%
    body_blocks.sort(key=lambda b: (b["left_pct"], b["top_pct"]))
    columns: list[list[dict[str, Any]]] = []
    for blk in body_blocks:
        placed = False
        for col in columns:
            ref_left = col[0]["left_pct"]
            if abs(blk["left_pct"] - ref_left) < 5.0:
                col.append(blk)
                placed = True
                break
        if not placed:
            columns.append([blk])

    # Within each column, sort by top_pct and merge adjacent blocks
    merged_body: list[dict[str, Any]] = []
    for col in columns:
        col.sort(key=lambda b: b["top_pct"])
        groups: list[list[dict[str, Any]]] = []
        current_group: list[dict[str, Any]] = [col[0]]

        for blk in col[1:]:
            last = current_group[-1]
            last_bottom = last["top_pct"] + last["height_pct"]
            gap = blk["top_pct"] - last_bottom
            if gap < 1.5:  # allow small gaps between lines
                current_group.append(blk)
            else:
                groups.append(current_group)
                current_group = [blk]
        groups.append(current_group)

        for grp in groups:
            if len(grp) == 1:
                merged_body.append(grp[0])
                continue

            top = min(b["top_pct"] for b in grp)
            left = min(b["left_pct"] for b in grp)
            bottom = max(b["top_pct"] + b["height_pct"] for b in grp)
            right = max(b["left_pct"] + b["width_pct"] for b in grp)

            texts = [b.get("en_text", "") for b in grp]
            combined_text = " ".join(t.strip() for t in texts if t.strip())

            merged_body.append({
                "top_pct": top,
                "left_pct": left,
                "width_pct": right - left,
                "height_pct": bottom - top,
                "role": "body",
                "bg_color": grp[0].get("bg_color", "#ffffff"),
                "text_color": grp[0].get("text_color", "#000000"),
                "en_text": combined_text,
            })

    # Combine non-body + merged body, sort by position
    result = non_body + merged_body
    result.sort(key=lambda b: (b["top_pct"], b["left_pct"]))
    return result


def _prepare_render_blocks(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Extract render-ready blocks from articles.

    Each block already has text_en from the translator.
    Builds a flat list of overlay blocks per page with these fixes:

    1. Skip bullet/symbol-only blocks (e.g. single 'l' bullet markers).
    2. Deduplicate: skip body blocks whose text is already fully
       contained inside a large subheadline block in the same article
       (this prevents quote + attribution showing twice when the quote
       block bbox already contains the body lines).
    3. Promote large subheadline blocks (tall multi-line quote boxes)
       to role 'body' so they get body styling instead of bold red.
    """
    for page in pages:
        for article in page.get("articles", []):
            blocks = article.get("blocks", [])
            render_blocks = []

            # Collect large subheadline bbox areas (quote containers)
            # A subheadline with height > 3x its font_size is really
            # a multi-line quote/paragraph, not a true subheadline.
            large_subheadline_texts: set[str] = set()
            for blk in blocks:
                if blk.get("role") == "subheadline":
                    h_px = blk.get("height_pct", 0)
                    w_px = blk.get("width_pct", 0)
                    # If the block is very tall & wide, it's a quote/paragraph block
                    if h_px * w_px > 300:   # rough area threshold in pct²
                        large_subheadline_texts.add(
                            (blk.get("text_en") or blk.get("text") or "").strip()
                        )

            for blk in blocks:
                text_en = (blk.get("text_en") or "").strip()
                if not text_en:
                    continue

                role = blk.get("role", "body")

                # Skip pure bullet/symbol blocks
                if _is_short_symbol(text_en):
                    continue

                # Demote red-colored headlines to subheadline.
                # Red text in the PDF is always a subheadline/kicker,
                # never the main headline, even if the font is large.
                if role == "headline":
                    tc = (blk.get("text_color") or "#000000").lower()
                    r = int(tc[1:3], 16) if len(tc) == 7 else 0
                    g = int(tc[3:5], 16) if len(tc) == 7 else 0
                    b_val = int(tc[5:7], 16) if len(tc) == 7 else 0
                    if r > 200 and g < 80 and b_val < 80:
                        role = "subheadline"

                # Promote large multi-line subheadlines to body role
                # so they get body font styling (not bold/red)
                if role == "subheadline":
                    h_px = blk.get("height_pct", 0)
                    w_px = blk.get("width_pct", 0)
                    if h_px * w_px > 300:
                        role = "body"

                # Deduplicate: skip body blocks whose content is already
                # fully inside a large subheadline block above it.
                if role == "body":
                    already_covered = False
                    for large_text in large_subheadline_texts:
                        if text_en and large_text and (
                            text_en in large_text or
                            # Also skip if the body block's text is a
                            # sub-sentence of the quote attribution
                            (len(text_en) > 10 and text_en in large_text)
                        ):
                            already_covered = True
                            break
                    if already_covered:
                        continue

                render_blocks.append({
                    "top_pct": blk["top_pct"],
                    "left_pct": blk["left_pct"],
                    "width_pct": blk["width_pct"],
                    "height_pct": blk["height_pct"],
                    "role": role,
                    "bg_color": blk.get("bg_color", "#ffffff"),
                    "text_color": blk.get("text_color", "#000000"),
                    "en_text": text_en,
                })

            # Merge consecutive body blocks in the same column
            # into larger blocks so text has room to flow.
            merged_render_blocks = _merge_body_blocks(render_blocks)
            article["render_blocks"] = merged_render_blocks

    return pages


def render_epaper(date_str: str) -> str:
    """
    Read articles_translated.json, render epaper.html.
    Returns the path to the generated HTML file.
    """
    in_json = os.path.join(DATA_DIR, date_str, "articles_translated.json")
    out_dir = os.path.join(OUTPUT_DIR, date_str)
    out_html = os.path.join(out_dir, "epaper.html")

    if not os.path.exists(in_json):
        raise FileNotFoundError(f"Input not found: {in_json}")

    with open(in_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    os.makedirs(out_dir, exist_ok=True)

    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    template = env.get_template("epaper_v3.html.j2")

    pages = _prepare_render_blocks(data.get("pages", []))

    # Embed each page image as a base64 data URI so the HTML is
    # fully self-contained and can be shared without the images folder.
    for page in pages:
        img_rel = page.get("image", "")          # e.g. "images/page_1.jpg"
        img_abs = os.path.join(out_dir, img_rel)  # output/{date}/images/page_1.jpg
        page["image_src"] = _embed_image(img_abs)

    total_blocks = sum(
        len(a.get("render_blocks", []))
        for p in pages for a in p.get("articles", [])
    )

    html = template.render(
        date=date_str,
        pages=pages,
    )

    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)

    size_mb = os.path.getsize(out_html) / (1024 * 1024)
    print(f"  HTML -> {out_html}  ({total_blocks} text overlays, {size_mb:.1f} MB)")
    return out_html


if __name__ == "__main__":
    import sys
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-03-02"
    render_epaper(date)
