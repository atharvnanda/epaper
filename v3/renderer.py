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
import re
from typing import Any

from jinja2 import Environment, FileSystemLoader

DATA_DIR = "data"
OUTPUT_DIR = "output"
TEMPLATE_DIR = "templates"

# ---------------------------------------------------------------------------
# Reference page dimensions (at CSS render width of 1120px).
# Used for headline font-size auto-fitting.
# ---------------------------------------------------------------------------
_RENDER_WIDTH_PX = 1120
_DEFAULT_ASPECT = 6134 / 3450   # typical Aaj Tak page
_RENDER_HEIGHT_PX = _RENDER_WIDTH_PX * _DEFAULT_ASPECT  # ~1991 px

# Known brand taglines that should NOT be translated / overlaid
_BRAND_TAGLINES = re.compile(
    r"^(सबसे\s*तेज़?\s*$)",
    re.IGNORECASE,
)

# Regex matching standalone "PAGE-XX" labels
_PAGE_RE = re.compile(r"^PAGE\s*-\s*\d+$", re.IGNORECASE)
# Regex matching PAGE-XX anywhere in text
_PAGE_CONTAINS_RE = re.compile(r"PAGE\s*-\s*\d+", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Banner / navbar detection
# ---------------------------------------------------------------------------
_BANNER_TOP_PCT_LIMIT = 15.0
_NEUTRAL_BACKGROUNDS = {"#ffffff", "#f8f8f8", "#f9f9f9", "#fafafa", "#eeeeee"}


def _is_banner_block(block: dict[str, Any]) -> bool:
    """Return True if this block sits in the coloured banner/navbar region."""
    if block["top_pct"] >= _BANNER_TOP_PCT_LIMIT:
        return False
    bg = (block.get("bg_color") or "#ffffff").lower()
    return bg not in _NEUTRAL_BACKGROUNDS


def _fit_banner_font_size(block: dict[str, Any]) -> int:
    """
    Compute a font size (px) that fills a banner block nicely.
    Only used for small navbar/banner blocks — regular article blocks
    keep their CSS role-based sizes or headline auto-fit.
    Clamped between 10–28 px.
    """
    MIN_FS, MAX_FS = 10, 28
    text = (block.get("en_text") or "").strip()
    text_len = len(text)
    if text_len == 0:
        return 14

    w_px = block["width_pct"] / 100.0 * _RENDER_WIDTH_PX
    h_px = block["height_pct"] / 100.0 * _RENDER_HEIGHT_PX
    usable_w = max(w_px - 4, 10)
    usable_h = max(h_px - 4, 10)

    margin = 1.15
    best = MIN_FS
    for fs in range(MIN_FS, MAX_FS + 1):
        char_w = fs * 0.55
        line_h = fs * 1.25
        chars_per_line = usable_w / char_w
        num_lines = usable_h / line_h
        capacity = chars_per_line * num_lines
        if capacity >= text_len * margin:
            best = fs
        else:
            break
    return best


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
    quote_chars = set('"""\u201c\u201d\u2018\u2019\u201e\u201a\xab\xbb\u2039\u203a\'')
    if all(c in quote_chars for c in stripped):
        return True
    return False


def _is_all_english(text: str) -> bool:
    """
    Return True if the original (Hindi) text is already entirely in
    English / Latin script (ASCII letters, digits, punctuation, spaces).

    When the original PDF text is already English there is no need to
    overlay a translation — the underlying page image already shows the
    correct text.  Skipping these blocks avoids covering elements like
    PAGE-07, PAGE-12, www.aajtak.in, etc. with an unnecessary overlay
    that may not fit the exact visual style.
    """
    # Allow: basic Latin (ASCII), common punctuation, whitespace
    for ch in text:
        if ord(ch) > 255:          # anything above Latin-1 → not pure English
            return False
    return True


def _fit_headline_font_size(block: dict[str, Any]) -> int:
    """
    Compute the largest font size (px) that fits the headline text
    inside its bounding box at the standard 1120px render width.

    Only used for headline blocks — body/subheadline keep CSS defaults.
    Clamped between 14–42 px.
    """
    MIN_FS, MAX_FS = 14, 42

    text = (block.get("en_text") or "").strip()
    text_len = len(text)
    if text_len == 0:
        return 30  # CSS default

    w_px = block["width_pct"] / 100.0 * _RENDER_WIDTH_PX
    h_px = block["height_pct"] / 100.0 * _RENDER_HEIGHT_PX

    usable_w = max(w_px - 6, 10)
    usable_h = max(h_px - 4, 10)

    margin = 1.15  # 15% safety margin for word-wrap variance
    best = MIN_FS

    for fs in range(MIN_FS, MAX_FS + 1):
        char_w = fs * 0.55       # average char width for proportional serif
        line_h = fs * 1.15       # headlines have tight line-height
        chars_per_line = usable_w / char_w
        num_lines = usable_h / line_h
        capacity = chars_per_line * num_lines
        if capacity >= text_len * margin:
            best = fs
        else:
            break

    return best


def _trim_headline_heights(render_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Trim headline block heights so they don't overlap the block below.

    The PDF often gives headline bounding boxes taller than the actual
    text area, causing the solid-background overlay to cover part of
    the photo or byline that sits just below the headline text.

    For each headline, if the next block (by top_pct) starts *inside*
    or very close to the headline's bottom, we shrink the headline
    height to end just above that next block.
    """
    if not render_blocks:
        return render_blocks

    # Sort by vertical position
    sorted_blocks = sorted(render_blocks, key=lambda b: b["top_pct"])

    result = []
    for i, blk in enumerate(sorted_blocks):
        role = blk.get("role", "body")
        if role != "headline":
            result.append(blk)
            continue

        hl_top = blk["top_pct"]
        hl_bottom = hl_top + blk["height_pct"]
        hl_left = blk["left_pct"]
        hl_right = hl_left + blk["width_pct"]

        # Find the next block that sits below this headline and
        # overlaps it horizontally (i.e. same column / article region)
        next_top = None
        for j in range(i + 1, len(sorted_blocks)):
            other = sorted_blocks[j]
            o_top = other["top_pct"]
            o_left = other["left_pct"]
            o_right = o_left + other["width_pct"]

            # Must start at or below the headline's top
            if o_top <= hl_top:
                continue

            # Must overlap horizontally (same column area)
            if o_right < hl_left or o_left > hl_right:
                continue

            next_top = o_top
            break

        if next_top is not None and next_top < hl_bottom:
            # Trim: headline ends just above the next block
            new_height = next_top - hl_top
            if new_height > 0.5:  # keep at least 0.5% height
                blk = {**blk, "height_pct": new_height}

        result.append(blk)

    return result


def _resolve_horizontal_overlaps(render_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Trim widths of overlapping render blocks so they don't cover each other.

    When two blocks sit at similar vertical positions (same band) and one
    block's right edge extends past another block's left edge, trim the
    wider/leftmost block's width so it ends at the other block's left edge.

    This prevents issues like:
      - A quote attribution overlay extending into an adjacent quote box
      - Two side-by-side blocks overlapping due to generous bounding boxes
    """
    if len(render_blocks) < 2:
        return render_blocks

    VERT_TOLERANCE = 2.0  # % — blocks within this are "same vertical band"

    for i, a in enumerate(render_blocks):
        a_top = a["top_pct"]
        a_bottom = a_top + a["height_pct"]
        a_left = a["left_pct"]
        a_right = a_left + a["width_pct"]

        for j, b in enumerate(render_blocks):
            if i == j:
                continue

            b_top = b["top_pct"]
            b_bottom = b_top + b["height_pct"]
            b_left = b["left_pct"]
            b_right = b_left + b["width_pct"]

            # Check if they overlap vertically (same band)
            v_overlap = min(a_bottom, b_bottom) - max(a_top, b_top)
            if v_overlap < VERT_TOLERANCE:
                continue

            # Check if they overlap horizontally
            h_overlap = min(a_right, b_right) - max(a_left, b_left)
            if h_overlap <= 0:
                continue

            # They overlap — trim the one whose right edge extends further
            # to the right, by shrinking its width so it ends at the other's
            # left edge.  If block A is to the left, trim A's right edge.
            if a_left < b_left and a_right > b_left:
                # A extends past B's left — trim A
                new_width = b_left - a_left
                if new_width > 1.0:  # keep at least 1% width
                    a["width_pct"] = new_width
                    a_right = a_left + new_width
            elif b_left < a_left and b_right > a_left:
                # B extends past A's left — trim B (handled when B is 'a')
                pass

    return render_blocks


def _merge_adjacent_blocks(
    blocks: list[dict[str, Any]],
    merge_roles: set[str] = frozenset({"body", "subheadline"}),
) -> list[dict[str, Any]]:
    """
    Merge adjacent same-role blocks into single blocks with concatenated text.

    Two kinds of merging are performed:

    A) HORIZONTAL merge (headlines and subheadlines only):
       Headline/subheadline blocks at the same vertical position (top_pct
       within 0.3%) that are horizontally adjacent (gap < 5%) are merged --
       but ONLY if the combined span exceeds 40% of page width.  This
       catches headlines the PDF parser split into two text spans, while
       avoiding merging separate columns of body text.

    B) VERTICAL merge (same column):
       Same-column blocks are merged vertically, only for roles in
       `merge_roles` (body, subheadline).
       Gap thresholds differ by role:
         - body: 1.5% (generous -- body text flows continuously)
         - subheadline: 0.5% (tight -- preserves bullet-point group boundaries)

    Strategy:
      1. Horizontal merge first (headlines/subheadlines with span check).
      2. Then vertical merge for body/subheadline roles.
    """
    if not blocks:
        return blocks

    # -- Step 1: Horizontal merge (same row, adjacent horizontally) --
    blocks = _merge_horizontal(blocks)

    # -- Step 2: Vertical merge (same column, adjacent vertically) --
    blocks = _merge_vertical(blocks, merge_roles)

    return blocks


def _merge_horizontal(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Merge blocks that sit at the same vertical position and are
    horizontally adjacent into wider blocks.

    CONSERVATIVE approach: only merge headline and subheadline blocks that,
    when combined, span a large portion of the page width (> 40%).  This
    catches headlines the PDF parser split into multiple spans while avoiding
    merging separate columns of body text that happen to sit at the same
    y-position.

    Body blocks are NOT merged horizontally -- they are handled by vertical
    merging instead, which is column-aware.
    """
    if not blocks:
        return blocks

    TOP_TOLERANCE = 0.3   # % -- blocks within this are "same row"
    H_GAP_MAX = 5.0       # % -- max horizontal gap to merge across
    MIN_SPAN_PCT = 40.0   # % -- merged result must span at least this wide
    MERGE_H_ROLES = {"headline", "subheadline"}

    # Only attempt horizontal merge on headline/subheadline blocks
    to_merge = [b for b in blocks if b.get("role") in MERGE_H_ROLES]
    others = [b for b in blocks if b.get("role") not in MERGE_H_ROLES]

    if not to_merge:
        return blocks

    # Sort by top_pct first, then left_pct
    sorted_hl = sorted(to_merge, key=lambda b: (b["top_pct"], b["left_pct"]))

    # Group into rows by top_pct proximity
    rows: list[list[dict[str, Any]]] = []
    current_row: list[dict[str, Any]] = [sorted_hl[0]]

    for blk in sorted_hl[1:]:
        if abs(blk["top_pct"] - current_row[0]["top_pct"]) <= TOP_TOLERANCE:
            current_row.append(blk)
        else:
            rows.append(current_row)
            current_row = [blk]
    rows.append(current_row)

    result: list[dict[str, Any]] = []

    for row in rows:
        if len(row) == 1:
            result.append(row[0])
            continue

        # Sort by left_pct within the row
        row.sort(key=lambda b: b["left_pct"])

        # Try to merge consecutive blocks with same role + bg_color + small gap,
        # but only keep the merge if the result is wide enough (span check).
        merged_group: list[dict[str, Any]] = [row[0]]

        for blk in row[1:]:
            last = merged_group[-1]
            last_right = last["left_pct"] + last["width_pct"]
            gap = blk["left_pct"] - last_right

            same_bg = blk.get("bg_color", "") == last.get("bg_color", "")
            same_role = blk.get("role") == last.get("role")

            # Check if merging would produce a wide-enough span
            new_left = min(last["left_pct"], blk["left_pct"])
            new_right = max(last_right, blk["left_pct"] + blk["width_pct"])
            merged_span = new_right - new_left

            if same_role and same_bg and -0.5 <= gap <= H_GAP_MAX and merged_span >= MIN_SPAN_PCT:
                # Merge: extend the last block to cover both
                new_top = min(last["top_pct"], blk["top_pct"])
                new_bottom = max(
                    last["top_pct"] + last["height_pct"],
                    blk["top_pct"] + blk["height_pct"],
                )

                last_text = last.get("en_text", "").strip()
                blk_text = blk.get("en_text", "").strip()
                combined_text = f"{last_text} {blk_text}".strip()

                merged_group[-1] = {
                    "top_pct": new_top,
                    "left_pct": new_left,
                    "width_pct": new_right - new_left,
                    "height_pct": new_bottom - new_top,
                    "role": last.get("role", "headline"),
                    "bg_color": last.get("bg_color", "#ffffff"),
                    "text_color": last.get("text_color", "#000000"),
                    "en_text": combined_text,
                }
            else:
                merged_group.append(blk)

        result.extend(merged_group)

    # Combine with non-headline/subheadline blocks
    result.extend(others)
    result.sort(key=lambda b: (b["top_pct"], b["left_pct"]))
    return result


def _merge_vertical(
    blocks: list[dict[str, Any]],
    merge_roles: set[str] = frozenset({"body", "subheadline"}),
) -> list[dict[str, Any]]:
    """
    Merge vertically adjacent same-role blocks in the same column.
    Only merges roles in `merge_roles`.
    """
    if not blocks:
        return blocks

    GAP_THRESHOLDS = {
        "body": 1.5,
        "subheadline": 0.5,
    }

    # Separate into mergeable (per-role) and non-mergeable
    keep: list[dict[str, Any]] = []
    by_role: dict[str, list[dict[str, Any]]] = {}
    for blk in blocks:
        role = blk.get("role", "body")
        if role in merge_roles:
            by_role.setdefault(role, []).append(blk)
        else:
            keep.append(blk)

    if not by_role:
        return blocks

    merged_all: list[dict[str, Any]] = list(keep)

    for role, role_blocks in by_role.items():
        max_gap = GAP_THRESHOLDS.get(role, 1.5)

        # Group by column using left_pct proximity
        role_blocks.sort(key=lambda b: (b["left_pct"], b["top_pct"]))
        columns: list[list[dict[str, Any]]] = []
        for blk in role_blocks:
            placed = False
            for col in columns:
                ref_left = col[0]["left_pct"]
                if abs(blk["left_pct"] - ref_left) < 5.0:
                    col.append(blk)
                    placed = True
                    break
            if not placed:
                columns.append([blk])

        # Within each column, merge vertically adjacent blocks
        for col in columns:
            col.sort(key=lambda b: b["top_pct"])
            groups: list[list[dict[str, Any]]] = []
            current_group: list[dict[str, Any]] = [col[0]]

            for blk in col[1:]:
                last = current_group[-1]
                last_bottom = last["top_pct"] + last["height_pct"]
                gap = blk["top_pct"] - last_bottom

                # Break group if: big gap or different bg_color
                same_bg = blk.get("bg_color", "") == last.get("bg_color", "")

                if gap < max_gap and same_bg:
                    current_group.append(blk)
                else:
                    groups.append(current_group)
                    current_group = [blk]
            groups.append(current_group)

            for grp in groups:
                if len(grp) == 1:
                    merged_all.append(grp[0])
                    continue

                top = min(b["top_pct"] for b in grp)
                left = min(b["left_pct"] for b in grp)
                bottom = max(b["top_pct"] + b["height_pct"] for b in grp)
                right = max(b["left_pct"] + b["width_pct"] for b in grp)

                texts = [b.get("en_text", "") for b in grp]
                combined = " ".join(t.strip() for t in texts if t.strip())

                merged_all.append({
                    "top_pct": top,
                    "left_pct": left,
                    "width_pct": right - left,
                    "height_pct": bottom - top,
                    "role": role,
                    "bg_color": grp[0].get("bg_color", "#ffffff"),
                    "text_color": grp[0].get("text_color", "#000000"),
                    "en_text": combined,
                })

    merged_all.sort(key=lambda b: (b["top_pct"], b["left_pct"]))
    return merged_all


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
        # ── Pre-scan: find the top_pct of standalone PAGE-XX blocks
        #    on this page so we can trim banner overlays above them.
        page_label_tops: list[float] = []
        for article in page.get("articles", []):
            for blk in article.get("blocks", []):
                orig = (blk.get("text") or "").strip()
                if orig and _is_all_english(orig) and _PAGE_RE.match(orig):
                    page_label_tops.append(blk["top_pct"])

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
                    if h_px * w_px > 300:   # rough area threshold in pct squared
                        large_subheadline_texts.add(
                            (blk.get("text_en") or blk.get("text") or "").strip()
                        )

            for blk in blocks:
                text_en = (blk.get("text_en") or "").strip()
                if not text_en:
                    continue

                # Skip blocks that were merged into a pointer group
                # (their text was combined into the group's first block)
                if blk.get("_pointer_merged"):
                    continue

                role = blk.get("role", "body")

                # Skip pure bullet/symbol blocks
                if _is_short_symbol(text_en):
                    continue

                # Skip blocks whose original text is already entirely
                # English — the page image already shows them correctly
                # (e.g. PAGE-07, www.aajtak.in, logos, English captions).
                original_text = (blk.get("text") or "").strip()
                if original_text and _is_all_english(original_text):
                    continue

                # Skip brand taglines (e.g. "सबसे तेज़") — these are
                # part of the Aaj Tak logo and should show through
                # from the underlying page image.
                if original_text and _BRAND_TAGLINES.match(original_text):
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

                # Expand pointer group leader blocks to cover all
                # fragments in the group (translator merged their text).
                blk_top = blk["top_pct"]
                blk_left = blk["left_pct"]
                blk_width = blk["width_pct"]
                blk_height = blk["height_pct"]
                if "_pointer_bottom_pct" in blk:
                    blk_height = blk["_pointer_bottom_pct"] - blk_top

                # For left/width: use pointer bounds BUT never start
                # further left than the leader block's own position.
                # The _pointer_left_pct may include bullet-marker blocks
                # whose left edge sits at the bullet position — using it
                # would cover the bullet point in the underlying image.
                if "_pointer_left_pct" in blk:
                    ptr_left = blk["_pointer_left_pct"]
                    # Only use the pointer left if it's NOT significantly
                    # to the left of the leader's own text position.
                    # A small leftward extension (< 1%) is OK (rounding),
                    # but larger means it includes a bullet marker.
                    if blk_left - ptr_left > 1.0:
                        # Pointer left includes bullet area — keep the
                        # leader's own left edge to preserve the bullet
                        pass
                    else:
                        blk_left = ptr_left
                if "_pointer_width_pct" in blk:
                    # Recalculate width to cover all text fragments,
                    # using the (possibly adjusted) left edge.
                    ptr_right = blk.get("_pointer_left_pct", blk["left_pct"]) + blk["_pointer_width_pct"]
                    blk_width = max(blk_width, ptr_right - blk_left)

                render_blocks.append({
                    "top_pct": blk_top,
                    "left_pct": blk_left,
                    "width_pct": blk_width,
                    "height_pct": blk_height,
                    "role": role,
                    "bg_color": blk.get("bg_color", "#ffffff"),
                    "text_color": blk.get("text_color", "#000000"),
                    "en_text": text_en,
                    "text_top_pct": blk.get("text_top_pct", blk["top_pct"]),
                })

            # ── Trim overlays for blocks with decorative elements ──
            # When a block starts with large decorative characters
            # (e.g. giant quote marks " " "), the block bbox starts
            # much higher than the actual text.  Trim the overlay so
            # it starts where the real text begins, leaving the
            # underlying decorative artwork visible.
            for rb in render_blocks:
                text_top = rb.get("text_top_pct", rb["top_pct"])
                block_top = rb["top_pct"]
                # Only trim if the decorative header is significant
                # (more than 1% of page height)
                if text_top - block_top > 1.0:
                    old_bottom = block_top + rb["height_pct"]
                    rb["top_pct"] = text_top
                    rb["height_pct"] = old_bottom - text_top

            # ── Banner block processing: trim height for blocks
            #    that contain PAGE-XX and fit font sizes ──
            for rb in render_blocks:
                if not _is_banner_block(rb):
                    continue
                # Trim banner blocks whose original source text
                # contained PAGE-XX: shrink height to end just
                # above the PAGE label so it shows through.
                blk_top = rb["top_pct"]
                blk_bottom = blk_top + rb["height_pct"]
                # Check if any PAGE label falls inside this block
                cutoffs = [
                    t for t in page_label_tops
                    if blk_top < t < blk_bottom
                ]
                if cutoffs:
                    trim_to = min(cutoffs)
                    if trim_to > blk_top:
                        rb["height_pct"] = trim_to - blk_top
                elif page_label_tops:
                    # No PAGE label inside, but check if one is nearby
                    nearby = [
                        t for t in page_label_tops
                        if abs(t - blk_bottom) < 3.0
                    ]
                    if nearby:
                        trim_to = min(nearby)
                        if trim_to > blk_top:
                            rb["height_pct"] = trim_to - blk_top

            # Trim headline heights so they don't overlap the
            # block below (photo / byline area).
            render_blocks = _trim_headline_heights(render_blocks)

            # Merge consecutive same-role blocks in the same column.
            merged_render_blocks = _merge_adjacent_blocks(render_blocks)

            # Resolve horizontal overlaps between blocks at similar
            # vertical positions (e.g. side-by-side quote attributions).
            merged_render_blocks = _resolve_horizontal_overlaps(merged_render_blocks)

            # Auto-fit font size:
            # - Banner blocks get banner-specific fitting (10–28px)
            # - Regular headline blocks get headline fitting (14–42px)
            for rb in merged_render_blocks:
                if _is_banner_block(rb):
                    rb["font_size_px"] = _fit_banner_font_size(rb)
                elif rb.get("role") == "headline":
                    rb["font_size_px"] = _fit_headline_font_size(rb)

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
