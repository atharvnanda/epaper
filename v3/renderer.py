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
    that shouldn't be rendered as a full overlay (e.g. 'l', '•', '»').
    """
    stripped = text.strip()
    return len(stripped) <= 2


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
                    "en_text": text_en,
                })

            article["render_blocks"] = render_blocks

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
