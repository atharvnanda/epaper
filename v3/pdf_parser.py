"""
v3 PDF parser -- article extractor for Aaj Tak epaper.

Approach:
  1. Extract rectangles from PDF drawings as hard article containers.
  2. Extract significant horizontal lines as band separators.
  3. Extract text blocks via get_text("dict") for font metadata + role.
  4. Assign blocks:
     a) If block center falls inside a rectangle -> that rect's article.
     b) Otherwise, assign to a band (between two h-line separators),
        then keep all blocks in the same band as ONE article (Option B).
  5. Each block keeps its coordinates + role for 1:1 block-level rendering.

Pipeline:  PDF -> rects + lines -> dict blocks -> assign -> JSON
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("PyMuPDF required: pip install PyMuPDF")


DATA_DIR = "data"
OUTPUT_DIR = "output"


# =====================================================================
# 1.  Extract drawing primitives (rectangles + lines)
# =====================================================================

def _extract_drawings(page):
    """
    Extract significant rectangles and lines from PDF drawings.

    Returns:
        rects:   list of {x0, y0, x1, y1, w, h}  -- article containers
        h_lines: list of {y, x0, x1, span}        -- horizontal separators
        v_lines: list of {x, y0, y1, span}        -- vertical separators
    """
    pw = float(page.rect.width)
    ph = float(page.rect.height)

    rects = []
    h_lines = []
    v_lines = []

    for drw in page.get_drawings():
        for item in drw["items"]:
            if item[0] == "re":
                r = item[1]
                rw = abs(r.x1 - r.x0)
                rh = abs(r.y1 - r.y0)
                # Only keep rectangles large enough to be article containers
                if rw > 80 and rh > 80:
                    rects.append({
                        "x0": round(min(r.x0, r.x1), 1),
                        "y0": round(min(r.y0, r.y1), 1),
                        "x1": round(max(r.x0, r.x1), 1),
                        "y1": round(max(r.y0, r.y1), 1),
                        "w": round(rw, 1),
                        "h": round(rh, 1),
                    })

            elif item[0] == "l":
                p1, p2 = item[1], item[2]
                dx = abs(p1.x - p2.x)
                dy = abs(p1.y - p2.y)
                # Horizontal lines (at least 15% page width)
                if dy < 3 and dx > pw * 0.15:
                    h_lines.append({
                        "y": round((p1.y + p2.y) / 2, 1),
                        "x0": round(min(p1.x, p2.x), 1),
                        "x1": round(max(p1.x, p2.x), 1),
                        "span": round(dx, 1),
                    })
                # Vertical lines (at least 5% page height)
                elif dx < 3 and dy > ph * 0.05:
                    v_lines.append({
                        "x": round((p1.x + p2.x) / 2, 1),
                        "y0": round(min(p1.y, p2.y), 1),
                        "y1": round(max(p1.y, p2.y), 1),
                        "span": round(dy, 1),
                    })

    rects.sort(key=lambda r: (r["y0"], r["x0"]))
    h_lines.sort(key=lambda l: l["y"])
    v_lines.sort(key=lambda l: l["x"])

    return rects, h_lines, v_lines


def _filter_container_rects(rects):
    """
    Filter rectangles to keep only meaningful article containers.
    Remove rects that are entirely contained inside a larger rect
    (those are photos/insets). Returns non-overlapping containers.
    """
    if not rects:
        return []

    # Sort by area descending -- process largest first
    by_area = sorted(rects, key=lambda r: r["w"] * r["h"], reverse=True)

    containers = []
    for r in by_area:
        is_inside = False
        for c in containers:
            if (r["x0"] >= c["x0"] - 5 and r["x1"] <= c["x1"] + 5 and
                r["y0"] >= c["y0"] - 5 and r["y1"] <= c["y1"] + 5):
                is_inside = True
                break
        if not is_inside:
            containers.append(r)

    return sorted(containers, key=lambda r: (r["y0"], r["x0"]))


def _get_band_separators(h_lines, pw):
    """
    Extract major horizontal band separators.
    Only lines spanning >= 50% page width are band separators.
    Returns sorted, deduplicated list of Y values.
    """
    threshold = pw * 0.50
    sep_ys = set()
    for line in h_lines:
        if line["span"] >= threshold:
            sep_ys.add(round(line["y"], 0))

    sep_list = sorted(sep_ys)
    if not sep_list:
        return []

    deduped = [sep_list[0]]
    for y in sep_list[1:]:
        if y - deduped[-1] > 15:
            deduped.append(y)

    return deduped


# =====================================================================
# 2.  Extract text blocks with font metadata
# =====================================================================

import re as _re

# Pattern: spans that are ONLY punctuation / quotes / symbols (no real text)
# Covers ASCII + Unicode curly quotes, dashes, bullets, etc.
_DECORATIVE_RE = _re.compile(
    r"^[\s"
    r"'\"«»"                           # ASCII quotes
    r"\u2018\u2019\u201A"               # Unicode single quotes ' '
    r"\u201C\u201D\u201E"               # Unicode double quotes " "
    r"\u2039\u203A"                     # single angle quotes ‹ ›
    r"\-\u2010\u2011\u2012\u2013\u2014\u2015\u2212"  # dashes/minus
    r"\u2026\u2022\u00B7\u00B0"         # ellipsis, bullets, degree
    r"|/\\()\[\]{}<>!?.,;:*#@~`\^"     # misc punctuation
    r"]+$"
)


def _is_decorative_span(text: str) -> bool:
    """Return True if a span contains only decorative / punctuation chars."""
    return bool(_DECORATIVE_RE.match(text.strip()))


def _classify_role(avg_size, max_size, fonts, flags):
    """Classify a text block's role from its font metadata."""
    font_str = " ".join(fonts).lower()
    is_bold = ("bold" in font_str or "extrabold" in font_str
               or "black" in font_str or "heavy" in font_str
               or flags & 16)

    if max_size >= 28:
        return "headline"
    if max_size >= 20:
        return "subheadline"
    if max_size >= 17 and is_bold:
        return "subheadline"
    if "extrabold" in font_str and max_size >= 14:
        return "byline"
    if max_size <= 12:
        return "caption"
    return "body"


def _extract_dict_blocks(page, page_idx, pw, ph):
    """
    Use get_text('dict') to extract text blocks with font metadata.

    Key improvements:
      - Ignores decorative/punctuation-only spans for font-size calculation
        (prevents giant quote marks from making a block look like a headline).
      - Splits blocks where lines have large X-gaps (> 40% page width)
        into separate sub-blocks (prevents PyMuPDF merging two side-by-side
        headlines into one wide block).

    Returns a list of block dicts with role classification.
    """
    d = page.get_text("dict")
    raw_blocks = []

    for bi, block in enumerate(d.get("blocks", []), 1):
        if block.get("type", 0) != 0:  # skip image blocks
            continue

        # Gather line-level data for potential splitting
        line_data = []  # list of (lx0, ly0, lx1, ly1, text, span_sizes, span_fonts, span_flags)
        for line in block.get("lines", []):
            line_text_parts = []
            sizes = []
            fonts = set()
            flags = 0
            lx0, ly0, lx1, ly1 = line["bbox"]
            for span in line.get("spans", []):
                txt = span.get("text", "")
                if txt.strip():
                    line_text_parts.append(txt)
                    # Only count non-decorative spans for font metrics
                    if not _is_decorative_span(txt):
                        sizes.append(span.get("size", 12.0))
                        fonts.add(span.get("font", ""))
                        flags |= span.get("flags", 0)
                    else:
                        # Still track font name for decorative spans
                        fonts.add(span.get("font", ""))
            if line_text_parts:
                line_data.append({
                    "x0": lx0, "y0": ly0, "x1": lx1, "y1": ly1,
                    "text": "".join(line_text_parts),
                    "sizes": sizes if sizes else [12.0],
                    "fonts": fonts,
                    "flags": flags,
                })

        if not line_data:
            continue

        # ---- Split detection: group lines by X-overlap ----
        # If lines within a single block occupy very different X ranges
        # (gap > 40% page width), split them into separate sub-blocks.
        x_gap_threshold = pw * 0.35

        line_groups = _split_lines_by_xgap(line_data, x_gap_threshold)

        for gi, group in enumerate(line_groups):
            all_text = "\n".join(ld["text"] for ld in group)
            if not all_text.strip():
                continue

            gx0 = min(ld["x0"] for ld in group)
            gy0 = min(ld["y0"] for ld in group)
            gx1 = max(ld["x1"] for ld in group)
            gy1 = max(ld["y1"] for ld in group)

            all_sizes = []
            all_fonts = set()
            all_flags = 0
            for ld in group:
                all_sizes.extend(ld["sizes"])
                all_fonts |= ld["fonts"]
                all_flags |= ld["flags"]

            avg_size = round(sum(all_sizes) / len(all_sizes), 1) if all_sizes else 12.0
            max_size = round(max(all_sizes), 1) if all_sizes else 12.0
            role = _classify_role(avg_size, max_size, all_fonts, all_flags)

            sub_id = f"p{page_idx}_b{bi}" if len(line_groups) == 1 else f"p{page_idx}_b{bi}_{gi}"

            raw_blocks.append({
                "id": sub_id,
                "text": all_text.strip(),
                "role": role,
                "font_size": avg_size,
                "max_font_size": max_size,
                "fonts": sorted(all_fonts),
                "x0": round(gx0, 2), "y0": round(gy0, 2),
                "x1": round(gx1, 2), "y1": round(gy1, 2),
                "top_pct": round(gy0 / ph * 100, 3),
                "left_pct": round(gx0 / pw * 100, 3),
                "width_pct": round((gx1 - gx0) / pw * 100, 3),
                "height_pct": round((gy1 - gy0) / ph * 100, 3),
            })

    return raw_blocks


def _split_lines_by_xgap(line_data, gap_threshold):
    """
    Split a list of line dicts into groups where lines in the same group
    have overlapping X ranges, and groups are separated by a large X gap.

    Uses a simple clustering: sort lines by X-center, then cut where
    the gap between consecutive X-centers exceeds gap_threshold.
    """
    if len(line_data) <= 1:
        return [line_data]

    # Sort by x-center
    sorted_lines = sorted(line_data, key=lambda ld: (ld["x0"] + ld["x1"]) / 2)

    groups = [[sorted_lines[0]]]
    for i in range(1, len(sorted_lines)):
        prev_center = (sorted_lines[i-1]["x0"] + sorted_lines[i-1]["x1"]) / 2
        curr_center = (sorted_lines[i]["x0"] + sorted_lines[i]["x1"]) / 2
        if curr_center - prev_center > gap_threshold:
            groups.append([])
        groups[-1].append(sorted_lines[i])

    return groups


# =====================================================================
# 3.  Assign blocks to articles
# =====================================================================

def _point_in_rect(cx, cy, rect, tol=3):
    """Check if point (cx, cy) is inside rect with tolerance."""
    return (rect["x0"] - tol <= cx <= rect["x1"] + tol and
            rect["y0"] - tol <= cy <= rect["y1"] + tol)


def _detect_column_boundaries(blocks, pw, min_gap=40):
    """
    Detect vertical column boundaries from X-gap analysis of blocks.

    Looks at x0/x1 intervals of all blocks.  Where there's a consistent
    X-gap (no block occupies that X-range), that's a column boundary.

    Returns sorted list of X-values that separate columns.
    """
    if len(blocks) < 2:
        return []

    # Only use "narrow" blocks for gap analysis (blocks < 60% page width)
    # Wide blocks (headlines) span columns and should NOT contribute to gap detection
    narrow_blocks = [b for b in blocks if (b["x1"] - b["x0"]) < pw * 0.55]
    if len(narrow_blocks) < 2:
        return []

    # Build x-coverage array (pixel resolution is fine for ~1242px width)
    coverage = [False] * (int(pw) + 2)
    for b in narrow_blocks:
        x0 = max(0, int(b["x0"]))
        x1 = min(int(pw), int(b["x1"]))
        for x in range(x0, x1 + 1):
            coverage[x] = True

    # Find gaps in coverage
    gaps = []
    in_gap = False
    gap_start = 0
    for x in range(int(pw) + 1):
        if not coverage[x]:
            if not in_gap:
                gap_start = x
                in_gap = True
        else:
            if in_gap:
                gap_width = x - gap_start
                if gap_width >= min_gap:
                    gaps.append((gap_start, x, gap_width))
                in_gap = False

    # Column boundaries are the midpoints of significant gaps
    boundaries = []
    for g_start, g_end, g_width in gaps:
        mid = (g_start + g_end) / 2
        # Skip gaps near page edges
        if mid < 30 or mid > pw - 30:
            continue
        boundaries.append(mid)

    return sorted(boundaries)


def _assign_block_to_column(block, col_boundaries, pw):
    """
    Given column boundaries [b1, b2, ...], determine which column
    a block belongs to based on its X-center.

    Returns column index (0, 1, 2, ...).
    """
    cx = (block["x0"] + block["x1"]) / 2
    for i, bnd in enumerate(col_boundaries):
        if cx < bnd:
            return i
    return len(col_boundaries)


def _block_spans_columns(block, col_boundaries, pw):
    """
    Check if a block spans multiple columns (i.e., it's wider than
    one column, crossing at least one boundary).

    Returns the set of column indices the block covers.
    """
    cols = set()
    # Check which boundaries the block crosses
    all_bounds = [0] + col_boundaries + [pw]
    for i in range(len(all_bounds) - 1):
        col_left = all_bounds[i]
        col_right = all_bounds[i + 1]
        # Does the block overlap this column?
        if block["x0"] < col_right - 5 and block["x1"] > col_left + 5:
            cols.add(i)
    return cols


def _split_band_into_articles(blocks, col_boundaries, h_lines_in_band, pw, ph):
    """
    Split a band's blocks into separate articles using column detection
    and headline-aware grouping.

    Strategy:
      1. Classify each block as "wide" (spans multiple columns) or narrow.
      2. Wide blocks (headlines/subheadlines) become article anchors.
         Each wide block owns all body/byline/caption blocks below it
         (in the columns it spans) until the next wide block or band end.
      3. Remaining narrow blocks are grouped per-column.
      4. Adjacent remaining column groups are merged ONLY if neither
         column has its own byline or headline (indicating separate articles).
         If a column has its own byline/headline, it stays separate.

    Returns list of article dicts (each with 'blocks' list).
    """
    if not col_boundaries:
        return [{"source": "band", "blocks": blocks}]

    num_cols = len(col_boundaries) + 1

    # ── 1. Separate wide vs narrow blocks ──
    wide_blocks = []          # (block, cols_spanned_set)
    narrow_by_col = {i: [] for i in range(num_cols)}

    for blk in blocks:
        cols_spanned = _block_spans_columns(blk, col_boundaries, pw)
        if len(cols_spanned) > 1:
            wide_blocks.append((blk, cols_spanned))
        else:
            col_idx = _assign_block_to_column(blk, col_boundaries, pw)
            narrow_by_col[col_idx].append(blk)

    for ci in narrow_by_col:
        narrow_by_col[ci].sort(key=lambda b: b["y0"])

    wide_blocks.sort(key=lambda wb: wb[0]["y0"])

    # ── 2. Build wide-block anchored articles ──
    consumed_ids = set()
    articles = []

    for wi, (wide_blk, cols_spanned) in enumerate(wide_blocks):
        # Lower boundary: next wide block y0, or band bottom
        next_wide_y = ph
        for wj in range(wi + 1, len(wide_blocks)):
            nwb = wide_blocks[wj][0]
            if nwb["y0"] > wide_blk["y1"] + 5:
                next_wide_y = nwb["y0"]
                break

        article_blocks = [wide_blk]

        for ci in cols_spanned:
            for blk in narrow_by_col.get(ci, []):
                if blk["id"] in consumed_ids:
                    continue
                cy = (blk["y0"] + blk["y1"]) / 2
                if wide_blk["y0"] - 5 <= cy <= next_wide_y + 5:
                    article_blocks.append(blk)
                    consumed_ids.add(blk["id"])

        articles.append({
            "source": "band",
            "blocks": sorted(article_blocks, key=lambda b: (b["y0"], b["x0"])),
        })

    # ── 3. Group remaining (un-anchored) narrow blocks per column ──
    remaining_col_groups = {}  # col_idx -> [blocks]
    for ci in range(num_cols):
        rem = [b for b in narrow_by_col[ci] if b["id"] not in consumed_ids]
        if rem:
            remaining_col_groups[ci] = rem

    # ── 4. Merge adjacent column groups ONLY if they don't each have
    #        their own headline/byline (which indicates separate articles) ──
    def _col_has_own_anchor(blks):
        """Check if a column group has its own byline or headline/subheadline."""
        for b in blks:
            if b["role"] in ("byline", "headline", "subheadline"):
                return True
        return False

    if remaining_col_groups:
        sorted_cols = sorted(remaining_col_groups.keys())
        merged_groups = []
        current_merge = [sorted_cols[0]]

        for i in range(1, len(sorted_cols)):
            prev_ci = sorted_cols[i - 1]
            curr_ci = sorted_cols[i]

            prev_blks = remaining_col_groups[prev_ci]
            curr_blks = remaining_col_groups[curr_ci]

            # Check Y-range overlap
            prev_y0 = min(b["y0"] for b in prev_blks)
            prev_y1 = max(b["y1"] for b in prev_blks)
            curr_y0 = min(b["y0"] for b in curr_blks)
            curr_y1 = max(b["y1"] for b in curr_blks)

            y_overlap = min(prev_y1, curr_y1) - max(prev_y0, curr_y0)

            # Merge conditions:
            # 1. Adjacent columns (no skipped column)
            # 2. Significant Y-overlap
            # 3. NEITHER column has its own byline/headline (else separate articles)
            prev_has_anchor = _col_has_own_anchor(prev_blks)
            curr_has_anchor = _col_has_own_anchor(curr_blks)

            should_merge = (
                curr_ci - prev_ci == 1 and
                y_overlap > 0.3 * min(prev_y1 - prev_y0, curr_y1 - curr_y0) and
                not prev_has_anchor and
                not curr_has_anchor
            )

            if should_merge:
                current_merge.append(curr_ci)
            else:
                merged_groups.append(current_merge)
                current_merge = [curr_ci]

        merged_groups.append(current_merge)

        # Each merged group becomes one article
        for col_group in merged_groups:
            grp_blocks = []
            for ci in col_group:
                grp_blocks.extend(remaining_col_groups[ci])
            articles.append({
                "source": "band",
                "blocks": sorted(grp_blocks, key=lambda b: (b["y0"], b["x0"])),
            })

    return articles


def _assign_blocks_to_articles(blocks, container_rects, band_seps,
                               h_lines, pw, ph):
    """
    Assign each text block to an article.

    Strategy:
      1. Try to assign to the SMALLEST enclosing rectangle (container).
      2. If no rectangle contains the block, assign to a horizontal band
         (between consecutive band separators).
      3. Within a band, use column detection to split into separate articles.

    Returns list of article dicts.
    """
    # Step 1: Assign to rectangle containers
    rect_articles = {i: [] for i in range(len(container_rects))}
    uncontained = []

    for blk in blocks:
        cx = (blk["x0"] + blk["x1"]) / 2
        cy = (blk["y0"] + blk["y1"]) / 2

        best_rect_idx = None
        best_area = float("inf")

        for ri, rect in enumerate(container_rects):
            if _point_in_rect(cx, cy, rect):
                area = rect["w"] * rect["h"]
                if area < best_area:
                    best_area = area
                    best_rect_idx = ri

        if best_rect_idx is not None:
            rect_articles[best_rect_idx].append(blk)
        else:
            uncontained.append(blk)

    # Step 2: Assign uncontained blocks to bands
    band_bounds = [0.0] + band_seps + [ph]
    band_buckets = {}
    for blk in uncontained:
        cy = (blk["y0"] + blk["y1"]) / 2
        band_idx = 0
        for bi in range(len(band_bounds) - 1):
            if band_bounds[bi] <= cy <= band_bounds[bi + 1]:
                band_idx = bi
                break
        band_buckets.setdefault(band_idx, []).append(blk)

    # Step 3: Build article list
    articles = []

    # Rect articles
    for ri, blks in rect_articles.items():
        if not blks:
            continue
        articles.append({
            "source": "rect",
            "container": container_rects[ri],
            "blocks": sorted(blks, key=lambda b: (b["y0"], b["x0"])),
        })

    # Band articles -- with column splitting
    for bi, blks in sorted(band_buckets.items()):
        if not blks:
            continue

        band_top = band_bounds[bi]
        band_bot = band_bounds[bi + 1]

        # Find h-lines within this band
        h_lines_in_band = [
            h for h in h_lines
            if band_top - 5 <= h["y"] <= band_bot + 5
        ]

        # Detect column boundaries from block X-positions
        col_boundaries = _detect_column_boundaries(blks, pw, min_gap=30)

        if col_boundaries:
            # Split band into column-based articles
            sub_articles = _split_band_into_articles(
                blks, col_boundaries, h_lines_in_band, pw, ph
            )
            for sa in sub_articles:
                sa["band_idx"] = bi
                articles.append(sa)
        else:
            # No columns detected -- keep as one article
            articles.append({
                "source": "band",
                "band_idx": bi,
                "blocks": sorted(blks, key=lambda b: (b["y0"], b["x0"])),
            })

    return articles


def _build_article_output(articles, page_idx, pw, ph):
    """
    Convert internal article structures to output format.
    """
    result = []
    for ai, art in enumerate(articles, 1):
        blks = art["blocks"]
        if not blks:
            continue

        top = min(b["y0"] for b in blks)
        bot = max(b["y1"] for b in blks)
        left = min(b["x0"] for b in blks)
        right = max(b["x1"] for b in blks)

        full_text = "\n".join(b["text"] for b in blks)

        result.append({
            "article_id": f"p{page_idx}_a{ai}",
            "source": art["source"],
            "top": round(top, 2),
            "left": round(left, 2),
            "width": round(right - left, 2),
            "height": round(bot - top, 2),
            "top_pct": round(top / ph * 100, 3),
            "left_pct": round(left / pw * 100, 3),
            "width_pct": round((right - left) / pw * 100, 3),
            "height_pct": round((bot - top) / ph * 100, 3),
            "block_count": len(blks),
            "text": full_text,
            "blocks": blks,
        })

    return result


# =====================================================================
# 4.  Main entry point
# =====================================================================

def parse_pdf(pdf_path: str, date_str: str | None = None, dpi: int = 200):
    """
    Parse one PDF file -> pages with articles and per-block text.
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    out_data = os.path.join(DATA_DIR, date_str)
    out_imgs = os.path.join(OUTPUT_DIR, date_str, "images")
    os.makedirs(out_data, exist_ok=True)
    os.makedirs(out_imgs, exist_ok=True)

    pdf_abs = str(Path(pdf_path).resolve())
    scale = max(72, dpi) / 72.0
    mat = fitz.Matrix(scale, scale)

    pages_out = []

    with fitz.open(pdf_abs) as doc:
        for page_idx, page in enumerate(doc, start=1):
            pw = float(page.rect.width)
            ph = float(page.rect.height)
            print(f"\n-- Page {page_idx}  ({pw:.0f}x{ph:.0f}) --")

            # Export image
            img_path = os.path.join(out_imgs, f"page_{page_idx}.jpg")
            pix = page.get_pixmap(matrix=mat, alpha=False)
            pix.save(img_path)
            print(f"  Image -> {img_path}")

            # 1. Drawing primitives
            all_rects, h_lines, v_lines = _extract_drawings(page)
            print(f"  Raw rects: {len(all_rects)}, H-lines: {len(h_lines)}, V-lines: {len(v_lines)}")

            # 2. Filter to container rects
            containers = _filter_container_rects(all_rects)
            print(f"  Container rects: {len(containers)}")

            # 3. Band separators
            band_seps = _get_band_separators(h_lines, pw)
            print(f"  Band separators: {band_seps}")

            # 4. Text blocks with font metadata
            blocks = _extract_dict_blocks(page, page_idx, pw, ph)
            print(f"  Text blocks: {len(blocks)}")

            # 5. Assign blocks to articles
            raw_articles = _assign_blocks_to_articles(
                blocks, containers, band_seps, h_lines, pw, ph
            )

            # 6. Build output
            articles = _build_article_output(raw_articles, page_idx, pw, ph)

            # Unassigned check
            assigned_ids = {b["id"] for a in articles for b in a["blocks"]}
            unassigned = [b for b in blocks if b["id"] not in assigned_ids]

            pages_out.append({
                "page_num": page_idx,
                "page_w": pw,
                "page_h": ph,
                "image": f"images/page_{page_idx}.jpg",
                "articles": articles,
                "unassigned": unassigned,
            })

            print(f"  Articles: {len(articles)} ({sum(a['block_count'] for a in articles)} blocks)")
            if unassigned:
                print(f"  ! Unassigned: {len(unassigned)}")

            # Article summary
            for a in articles:
                roles = {}
                for b in a["blocks"]:
                    roles[b["role"]] = roles.get(b["role"], 0) + 1
                role_str = ", ".join(f"{k}={v}" for k, v in sorted(roles.items()))
                print(f"    {a['article_id']} [{a['source']}]: {a['block_count']} blocks ({role_str})")

    result = {
        "date": date_str,
        "page_count": len(pages_out),
        "pages": pages_out,
    }

    json_path = os.path.join(out_data, "pdf_blocks.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n+ JSON -> {json_path}")

    total_blocks = sum(a["block_count"] for pg in pages_out for a in pg["articles"])
    total_unassigned = sum(len(pg["unassigned"]) for pg in pages_out)
    total_articles = sum(len(pg["articles"]) for pg in pages_out)
    print(f"  {result['page_count']} pages, {total_articles} articles, "
          f"{total_blocks} blocks assigned, {total_unassigned} unassigned")

    return result


# =====================================================================
# CLI
# =====================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PDF article extractor (v3)")
    parser.add_argument("pdf", nargs="?",
                        default=r"data\Aaj tak-Template-26.02.2026 PAGE-1.pdf",
                        help="Path to PDF file")
    parser.add_argument("date", nargs="?", default=None,
                        help="Date string YYYY-MM-DD (default: today)")
    parser.add_argument("--dpi", type=int, default=200,
                        help="Image export DPI (default 200)")
    args = parser.parse_args()
    parse_pdf(args.pdf, args.date, args.dpi)
