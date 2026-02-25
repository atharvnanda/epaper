"""
Step 2.5: OCR — Detect text regions on page images.

Scans each page JPG with easyocr and records the bounding boxes of every
text block.  For each article zone we then know *exactly* which sub-regions
are text (→ overlay) and which are photos/graphics (→ leave transparent).

Output: data/{date}/articles_ocr.json  — same structure as
articles_translated.json but every article gains a `text_blocks` list.
"""

import json
import os
import sys

import easyocr
import numpy as np
from PIL import Image

DATA_DIR = "data"
OUTPUT_DIR = "output"

# The scraper's pagerectangle coordinates live in a coordinate space that
# is smaller than the actual JPG pixel dimensions.  These constants are
# the observed maximums across all pages.
COORD_SPACE_W = 1128
COORD_SPACE_H = 2050


class _NumpyEncoder(json.JSONEncoder):
    """JSON encoder that handles numpy types."""
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


def _paths(date_str: str):
    data_dir = os.path.join(DATA_DIR, date_str)
    images_dir = os.path.join(OUTPUT_DIR, date_str, "images")
    translated_file = os.path.join(data_dir, "articles_translated.json")
    ocr_file = os.path.join(data_dir, "articles_ocr.json")
    return translated_file, ocr_file, images_dir


def _box_inside_zone(ocr_box, zone, margin=10):
    """Check if an OCR bounding box is (mostly) inside a zone rectangle.

    Both are dicts with keys: left, top, right, bottom  (image-pixel coords).
    margin: pixels of tolerance for boxes near zone edges.
    """
    # Centre of the OCR box
    cx = (ocr_box["left"] + ocr_box["right"]) / 2
    cy = (ocr_box["top"] + ocr_box["bottom"]) / 2
    return (zone["left"] - margin <= cx <= zone["right"] + margin and
            zone["top"] - margin <= cy <= zone["bottom"] + margin)


def _classify_blocks(blocks, zone_height_px):
    """Heuristic: label each block as headline / subheadline / body / byline.

    Uses the block height (proxy for font size) and vertical position.
    """
    if not blocks:
        return blocks

    max_h = max(b["height"] for b in blocks)

    for b in blocks:
        h = b["height"]
        rel_y = b["rel_top"]  # 0 = top of zone, 1 = bottom

        if h >= max_h * 0.7 and h > 25:
            b["role"] = "headline"
        elif h >= max_h * 0.45 and h > 18:
            b["role"] = "subheadline"
        elif rel_y > 0.92 or h < 12:
            b["role"] = "byline"
        else:
            b["role"] = "body"

    return blocks


def _page_already_done(pg):
    """Return True if every article on this page already has text_blocks."""
    articles = pg.get("articles", [])
    if not articles:
        return True
    return all("text_blocks" in a for a in articles)


def _save_incremental(data, ocr_file):
    """Write the current state to disk (atomic-ish via temp file)."""
    tmp = ocr_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, cls=_NumpyEncoder)
    # Rename is atomic on most filesystems
    if os.path.exists(ocr_file):
        os.replace(tmp, ocr_file)
    else:
        os.rename(tmp, ocr_file)


def _process_page(pg, images_dir, reader):
    """Run OCR on a single page and populate text_blocks for each article."""
    page_num = pg["page_num"]
    img_path = os.path.join(images_dir, f"page_{page_num}.jpg")

    if not os.path.exists(img_path):
        print(f"  Page {page_num}: image not found, skipping")
        return False

    # Get image dimensions for coordinate scaling
    with Image.open(img_path) as im:
        img_w, img_h = im.size

    scale_x = img_w / COORD_SPACE_W
    scale_y = img_h / COORD_SPACE_H

    # Run OCR on full page
    print(f"  Page {page_num}: running OCR on {img_path} ({img_w}x{img_h})...")
    ocr_results = reader.readtext(img_path)
    print(f"    Found {len(ocr_results)} text blocks")

    # Convert OCR results to a simpler list of dicts
    ocr_boxes = []
    for box_corners, text, conf in ocr_results:
        xs = [float(pt[0]) for pt in box_corners]
        ys = [float(pt[1]) for pt in box_corners]
        ocr_boxes.append({
            "left": min(xs),
            "top": min(ys),
            "right": max(xs),
            "bottom": max(ys),
            "width": max(xs) - min(xs),
            "height": max(ys) - min(ys),
            "text": text,
            "conf": round(float(conf), 3),
        })

    # For each article zone, find OCR boxes that fall inside
    for art in pg["articles"]:
        zone_px = {
            "left":   art["left"]  * scale_x,
            "top":    art["top"]   * scale_y,
            "right":  (art["left"] + art["width"])  * scale_x,
            "bottom": (art["top"]  + art["height"]) * scale_y,
        }
        zone_h_px = zone_px["bottom"] - zone_px["top"]

        matched = []
        for ob in ocr_boxes:
            if _box_inside_zone(ob, zone_px):
                matched.append({
                    "top_pct":    round(ob["top"]    / img_h * 100, 3),
                    "left_pct":   round(ob["left"]   / img_w * 100, 3),
                    "width_pct":  round(ob["width"]  / img_w * 100, 3),
                    "height_pct": round(ob["height"] / img_h * 100, 3),
                    "rel_top": round((ob["top"] - zone_px["top"]) / zone_h_px, 3)
                               if zone_h_px > 0 else 0,
                    "height": round(ob["height"], 1),
                    "ocr_text": ob["text"],
                    "conf": ob["conf"],
                })

        matched.sort(key=lambda b: b["top_pct"])
        _classify_blocks(matched, zone_h_px)
        art["text_blocks"] = matched

    total_blocks = sum(len(a.get("text_blocks", [])) for a in pg["articles"])
    print(f"    Mapped {total_blocks} text blocks to {len(pg['articles'])} article zones")
    return True


def run_ocr(date_str: str):
    """Main OCR pipeline — incremental, saves after each page.

    - If articles_ocr.json already exists, loads it and skips pages that
      are already done (all articles have text_blocks).
    - Saves to disk after every page so a crash never loses more than
      the page that was being processed.
    - Wraps each page in try/except so one bad page doesn't kill the run.
    """
    translated_file, ocr_file, images_dir = _paths(date_str)

    if not os.path.exists(translated_file):
        print(f"  ERROR: {translated_file} not found.  Run translator.py first.")
        return

    # ── Resume from existing OCR file if available ──
    if os.path.exists(ocr_file):
        print(f"  Found existing {ocr_file} — resuming from it.")
        with open(ocr_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        with open(translated_file, "r", encoding="utf-8") as f:
            data = json.load(f)

    pages = data["pages"]
    todo = [pg for pg in pages if not _page_already_done(pg)]

    if not todo:
        print(f"  All {len(pages)} pages already have OCR data — nothing to do.")
        print(f"  (Delete {ocr_file} to force a full re-run.)")
        return

    print(f"  {len(todo)}/{len(pages)} pages still need OCR.")

    # Initialise easyocr only if there's work to do
    print("  Initialising easyocr (Hindi + English)...")
    reader = easyocr.Reader(["hi", "en"], gpu=True, verbose=False)

    processed = 0
    failed = 0
    for pg in todo:
        page_num = pg["page_num"]
        try:
            ok = _process_page(pg, images_dir, reader)
            if ok:
                processed += 1
        except Exception as e:
            failed += 1
            print(f"  ⚠ Page {page_num}: ERROR — {type(e).__name__}: {e}")
            print(f"    Skipping this page, progress so far is safe.")

        # Save after every page (whether success or failure for other pages)
        _save_incremental(data, ocr_file)
        print(f"    💾 Saved progress ({processed} done, {failed} failed, "
              f"{len(todo) - processed - failed} remaining)")

    # Final summary
    total_zones = sum(len(pg["articles"]) for pg in pages)
    total_blocks = sum(
        len(a.get("text_blocks", []))
        for pg in pages
        for a in pg["articles"]
    )
    still_todo = sum(1 for pg in pages if not _page_already_done(pg))

    print(f"\n  Done!  Saved {ocr_file}")
    print(f"  Total article zones: {total_zones}")
    print(f"  Total text blocks mapped: {total_blocks}")
    print(f"  Pages processed this run: {processed}")
    if failed:
        print(f"  ⚠ Pages failed: {failed} — re-run to retry them.")
    if still_todo:
        print(f"  Pages still incomplete: {still_todo} — re-run to retry.")


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-02-25"
    run_ocr(date)
