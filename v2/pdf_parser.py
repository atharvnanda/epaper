"""
v2 PDF parser (hybrid mapping)

Reads a searchable e-paper PDF with PyMuPDF, exports each page as a JPG,
extracts page article zones (pagerectangles) via Playwright, and assigns PDF
text blocks to those zones using scaled coordinate mapping.

Output JSON is grouped per page -> articles (by storyid) + unassigned blocks.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

try:
    import fitz  # PyMuPDF
except Exception as exc:  # pragma: no cover
    raise RuntimeError("PyMuPDF is required. Install with: pip install PyMuPDF") from exc

try:
    from playwright.sync_api import sync_playwright
except Exception as exc:  # pragma: no cover
    raise RuntimeError("Playwright is required. Install with: pip install playwright") from exc


DATA_DIR = "data"
OUTPUT_DIR = "output"

# Scraped pagerectangles live in this fixed coordinate space (from V1).
COORD_SPACE_W = 1128
COORD_SPACE_H = 2050


def _default_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _mk_dirs(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _pct(value: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return round((value / total) * 100.0, 3)


def _parse_style(style_str: str) -> dict[str, float]:
    """Extract top, left, width, height from inline style string."""
    props: dict[str, float] = {}
    for prop in ("top", "left", "width", "height"):
        match = re.search(rf"{prop}:\s*([\d.]+)px", style_str)
        if match:
            props[prop] = float(match.group(1))
    return props


def _extract_pagerectangles(html: str) -> dict[int, list[dict[str, Any]]]:
    """Parse #ImageContainer slides and extract article pagerectangles only."""
    soup = BeautifulSoup(html, "lxml")
    container = soup.find(id="ImageContainer")
    if not container:
        raise RuntimeError("Could not find #ImageContainer in epaper HTML")

    slides = container.find_all("li", class_="mySlides")
    page_zones: dict[int, list[dict[str, Any]]] = {}

    for page_idx, slide in enumerate(slides, start=1):
        zones: list[dict[str, Any]] = []
        rectangles = slide.find_all("div", class_="pagerectangle")
        for rect in rectangles:
            storyid = (rect.get("storyid") or "").strip()
            style = rect.get("style", "")
            coords = _parse_style(style)

            if not storyid:
                continue
            if not all(k in coords for k in ("top", "left", "width", "height")):
                continue

            zones.append(
                {
                    "storyid": storyid,
                    "top": coords["top"],
                    "left": coords["left"],
                    "width": coords["width"],
                    "height": coords["height"],
                }
            )

        page_zones[page_idx] = zones

    return page_zones


def scrape_pagerectangles(
    epaper_url: str = "https://epaper.aajtak.in/",
    max_carousel_clicks: int = 12,
    click_wait_sec: float = 1.5,
    headless: bool = True,
) -> dict[int, list[dict[str, Any]]]:
    """Use Playwright to load epaper and extract page-level pagerectangles."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1400, "height": 900},
        )
        page = context.new_page()
        page.goto(epaper_url, timeout=60000)
        page.wait_for_selector("#ImageContainer", timeout=30000)
        time.sleep(2)

        # Click carousel to trigger lazy-loaded rectangles.
        for _ in range(max(0, max_carousel_clicks)):
            next_btn = page.query_selector("button.next")
            if not next_btn:
                break
            try:
                next_btn.click(timeout=5000)
                time.sleep(click_wait_sec)
            except Exception:
                break

        time.sleep(1)
        html = page.content()
        browser.close()

    return _extract_pagerectangles(html)


def _serialize_block(
    block: tuple[Any, ...],
    block_id: str,
    page_w: float,
    page_h: float,
) -> dict[str, Any] | None:
    """
    PyMuPDF block tuple from get_text("blocks") is typically:
      (x0, y0, x1, y1, text, block_no, block_type)
    """
    if len(block) < 5:
        return None

    x0, y0, x1, y1, text = block[:5]
    block_no = block[5] if len(block) > 5 else None
    block_type = block[6] if len(block) > 6 else None

    if not isinstance(text, str):
        return None

    cleaned = text.strip()
    if not cleaned:
        return None

    width = max(0.0, float(x1) - float(x0))
    height = max(0.0, float(y1) - float(y0))
    cx = (float(x0) + float(x1)) / 2.0
    cy = (float(y0) + float(y1)) / 2.0

    return {
        "block_id": block_id,
        "block_no": int(block_no) if isinstance(block_no, (int, float)) else block_no,
        "block_type": int(block_type) if isinstance(block_type, (int, float)) else block_type,
        "text": cleaned,
        "x0": round(float(x0), 3),
        "y0": round(float(y0), 3),
        "x1": round(float(x1), 3),
        "y1": round(float(y1), 3),
        "cx": round(cx, 3),
        "cy": round(cy, 3),
        "width": round(width, 3),
        "height": round(height, 3),
        "top_pct": _pct(float(y0), page_h),
        "left_pct": _pct(float(x0), page_w),
        "width_pct": _pct(width, page_w),
        "height_pct": _pct(height, page_h),
    }


def _box_inside_zone(box: dict[str, float], zone: dict[str, float], margin: float = 10.0) -> bool:
    """V1 logic: center-point containment with edge tolerance margin."""
    cx = (box["left"] + box["right"]) / 2
    cy = (box["top"] + box["bottom"]) / 2
    return (
        zone["left"] - margin <= cx <= zone["right"] + margin
        and zone["top"] - margin <= cy <= zone["bottom"] + margin
    )


def _scaled_zone(zone: dict[str, Any], page_w: float, page_h: float) -> dict[str, Any]:
    """Scale scraped zone from fixed coord-space to real PDF page coords."""
    scale_x = page_w / COORD_SPACE_W
    scale_y = page_h / COORD_SPACE_H

    left = float(zone["left"]) * scale_x
    top = float(zone["top"]) * scale_y
    width = float(zone["width"]) * scale_x
    height = float(zone["height"]) * scale_y
    right = left + width
    bottom = top + height

    return {
        "storyid": zone["storyid"],
        "coord_space": {
            "top": round(float(zone["top"]), 3),
            "left": round(float(zone["left"]), 3),
            "width": round(float(zone["width"]), 3),
            "height": round(float(zone["height"]), 3),
        },
        "scaled": {
            "left": round(left, 3),
            "top": round(top, 3),
            "right": round(right, 3),
            "bottom": round(bottom, 3),
            "width": round(width, 3),
            "height": round(height, 3),
            "top_pct": _pct(top, page_h),
            "left_pct": _pct(left, page_w),
            "width_pct": _pct(width, page_w),
            "height_pct": _pct(height, page_h),
        },
    }


def _assign_storyid(
    block: dict[str, Any],
    scaled_zones: list[dict[str, Any]],
    margin: float,
) -> str | None:
    """Assign block to zone with highest intersection percentage of block area."""
    box = {
        "left": float(block["x0"]),
        "top": float(block["y0"]),
        "right": float(block["x1"]),
        "bottom": float(block["y1"]),
    }
    block_w = max(0.0, box["right"] - box["left"])
    block_h = max(0.0, box["bottom"] - box["top"])
    block_area = block_w * block_h
    if block_area <= 0:
        return None

    best_sid: str | None = None
    best_overlap_pct = 0.0

    for zone in scaled_zones:
        z = zone["scaled"]
        zone_box = {
            "left": float(z["left"]),
            "top": float(z["top"]),
            "right": float(z["right"]),
            "bottom": float(z["bottom"]),
        }

        if not _box_inside_zone(box, zone_box, margin=margin):
            continue

        inter_left = max(box["left"], zone_box["left"])
        inter_top = max(box["top"], zone_box["top"])
        inter_right = min(box["right"], zone_box["right"])
        inter_bottom = min(box["bottom"], zone_box["bottom"])

        inter_w = max(0.0, inter_right - inter_left)
        inter_h = max(0.0, inter_bottom - inter_top)
        inter_area = inter_w * inter_h
        overlap_pct = inter_area / block_area

        if overlap_pct > best_overlap_pct:
            best_overlap_pct = overlap_pct
            best_sid = str(zone["storyid"])

    return best_sid


def _sort_article_blocks(blocks: list[dict[str, Any]], zone_scaled_width: float) -> list[dict[str, Any]]:
    """
    Column-aware block ordering with dynamic binning.

    Rules:
    1) Estimate column width from article zone width
    2) Assign dynamic col_bin by x0 / est_col_width
    3) Sort by (col_bin, y0)
    """
    if not blocks:
        return blocks

    est_col_width = max(1.0, float(zone_scaled_width) / 18.0)
    for block in blocks:
        block["col_bin"] = int(float(block.get("x0", 0.0)) / est_col_width)

    return sorted(blocks, key=lambda b: (int(b["col_bin"]), float(b["y0"])))


def parse_pdf(
    pdf_path: str,
    date_str: str | None = None,
    out_json: str | None = None,
    images_dir: str | None = None,
    dpi: int = 170,
    zone_margin: float = 10.0,
    epaper_url: str = "https://epaper.aajtak.in/",
    max_carousel_clicks: int = 12,
    no_scrape_zones: bool = False,
) -> dict[str, Any]:
    """Parse PDF blocks, map to article zones, and export JPG page backgrounds."""
    if date_str is None:
        date_str = _default_date()

    if out_json is None:
        out_json = os.path.join(DATA_DIR, date_str, "pdf_blocks.json")
    if images_dir is None:
        images_dir = os.path.join(OUTPUT_DIR, date_str, "images")

    _mk_dirs(os.path.dirname(out_json))
    _mk_dirs(images_dir)

    page_zones: dict[int, list[dict[str, Any]]] = {}
    if not no_scrape_zones:
        try:
            page_zones = scrape_pagerectangles(
                epaper_url=epaper_url,
                max_carousel_clicks=max_carousel_clicks,
            )
            print(f"Scraped article zones for {len(page_zones)} pages")
        except Exception as exc:
            print(f"WARN: zone scraping failed ({type(exc).__name__}: {exc}); all blocks will be unassigned")

    scale = max(72, dpi) / 72.0
    matrix = fitz.Matrix(scale, scale)

    pdf_abs = str(Path(pdf_path).resolve())
    pages: list[dict[str, Any]] = []

    with fitz.open(pdf_abs) as doc:
        for i, page in enumerate(doc, start=1):
            page_w = float(page.rect.width)
            page_h = float(page.rect.height)

            img_name = f"page_{i}.jpg"
            img_path = os.path.join(images_dir, img_name)

            pix = page.get_pixmap(matrix=matrix, alpha=False)
            pix.save(img_path)

            raw_blocks = page.get_text("blocks")
            serialized_blocks: list[dict[str, Any]] = []
            for b_idx, block in enumerate(raw_blocks, start=1):
                block_id = f"p{i}_b{b_idx}"
                parsed = _serialize_block(block, block_id, page_w, page_h)
                if parsed:
                    serialized_blocks.append(parsed)

            scraped_for_page = page_zones.get(i, [])
            scaled_zones = [_scaled_zone(z, page_w, page_h) for z in scraped_for_page]

            articles_map: dict[str, dict[str, Any]] = {}
            for z in scaled_zones:
                sid = str(z["storyid"])
                if sid not in articles_map:
                    articles_map[sid] = {"storyid": sid, "zones": [], "blocks": []}
                articles_map[sid]["zones"].append(z)

            unassigned: list[dict[str, Any]] = []

            for block in serialized_blocks:
                sid = _assign_storyid(block, scaled_zones, margin=zone_margin)
                if sid is None:
                    unassigned.append(block)
                else:
                    if sid not in articles_map:
                        articles_map[sid] = {"storyid": sid, "zones": [], "blocks": []}
                    articles_map[sid]["blocks"].append(block)

            for article in articles_map.values():
                zone_scaled_width = max(
                    (float(z["scaled"].get("width", 0.0)) for z in article["zones"]),
                    default=0.0,
                )
                article["blocks"] = _sort_article_blocks(article["blocks"], zone_scaled_width)

            unassigned.sort(key=lambda b: (float(b["y0"]), float(b["x0"])))

            articles = list(articles_map.values())

            def _article_sort_key(a: dict[str, Any]) -> tuple[float, float]:
                if a["blocks"]:
                    first = a["blocks"][0]
                    return float(first["y0"]), float(first["x0"])
                if a["zones"]:
                    z = a["zones"][0]["scaled"]
                    return float(z["top"]), float(z["left"])
                return (10_000_000.0, 10_000_000.0)

            articles.sort(key=_article_sort_key)

            pages.append(
                {
                    "page_num": i,
                    "page_width": round(page_w, 3),
                    "page_height": round(page_h, 3),
                    "image_local": img_path.replace("/", "\\"),
                    "articles": articles,
                    "unassigned": unassigned,
                }
            )

    result = {
        "date": date_str,
        "source_pdf": pdf_abs,
        "page_count": len(pages),
        "dpi": dpi,
        "coord_space": {
            "width": COORD_SPACE_W,
            "height": COORD_SPACE_H,
        },
        "zone_margin": zone_margin,
        "pages": pages,
    }

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse searchable PDF into article-grouped blocks JSON + JPG backgrounds")
    parser.add_argument("pdf", help="Path to source PDF")
    parser.add_argument("--date", default=None, help="Date namespace (YYYY-MM-DD), default=today")
    parser.add_argument("--out-json", default=None, help="Output JSON path (default: data/{date}/pdf_blocks.json)")
    parser.add_argument("--images-dir", default=None, help="Output images dir (default: output/{date}/images)")
    parser.add_argument("--dpi", type=int, default=170, help="JPG render DPI (default: 170)")
    parser.add_argument("--zone-margin", type=float, default=10.0, help="Center-inside-zone tolerance in PDF coords")
    parser.add_argument("--epaper-url", default="https://epaper.aajtak.in/", help="Source URL for pagerectangle scraping")
    parser.add_argument("--max-carousel-clicks", type=int, default=12, help="How many next-button clicks to preload slides")
    parser.add_argument("--no-scrape-zones", action="store_true", help="Skip Playwright zone scrape (all blocks become unassigned)")
    args = parser.parse_args()

    result = parse_pdf(
        pdf_path=args.pdf,
        date_str=args.date,
        out_json=args.out_json,
        images_dir=args.images_dir,
        dpi=args.dpi,
        zone_margin=args.zone_margin,
        epaper_url=args.epaper_url,
        max_carousel_clicks=args.max_carousel_clicks,
        no_scrape_zones=args.no_scrape_zones,
    )

    total_blocks = 0
    total_assigned = 0
    for pg in result["pages"]:
        assigned_here = sum(len(a.get("blocks", [])) for a in pg.get("articles", []))
        unassigned_here = len(pg.get("unassigned", []))
        total_assigned += assigned_here
        total_blocks += assigned_here + unassigned_here

    print(f"Done: {result['page_count']} pages, {total_blocks} text blocks")
    print(f"Assigned to stories: {total_assigned}, unassigned: {total_blocks - total_assigned}")
    print(f"JSON: {args.out_json or os.path.join(DATA_DIR, result['date'], 'pdf_blocks.json')}")
    print(f"Images: {args.images_dir or os.path.join(OUTPUT_DIR, result['date'], 'images')}")


if __name__ == "__main__":
    main()
