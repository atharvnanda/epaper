"""
Aaj Tak Epaper -> English Translation Pipeline (v3)

Pipeline:  PDF -> parse articles -> translate Hindi->English -> render HTML

Usage:
    python main.py                                            # default PDF + today's date
    python main.py "data/Aaj tak-Template-26.02.2026 PAGE-1.pdf"         # custom PDF
    python main.py "data/Aaj tak-Template-26.02.2026 PAGE-1.pdf" 2026-02-26  # custom PDF + date
    python main.py --skip-translate ...                       # skip translation step
"""

from __future__ import annotations

import argparse
import sys
import time

from v3.pdf_parser import parse_pdf
from v3.translator import translate_articles
from v3.renderer import render_epaper


def main():
    parser = argparse.ArgumentParser(
        description="Aaj Tak Epaper -> English Translation Pipeline"
    )
    parser.add_argument(
        "pdf", nargs="?",
        default=r"data\Aaj tak-Template-26.02.2026 PAGE-1.pdf",
        help="Path to the PDF file",
    )
    parser.add_argument(
        "date", nargs="?", default=None,
        help="Date string YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--dpi", type=int, default=200,
        help="Image export DPI (default: 200)",
    )
    parser.add_argument(
        "--skip-translate", action="store_true",
        help="Skip the translation step (use Hindi text as-is)",
    )
    parser.add_argument(
        "--skip-parse", action="store_true",
        help="Skip the PDF parse step (re-use existing pdf_blocks.json)",
    )
    args = parser.parse_args()

    print("=" * 50)
    print("  Aaj Tak Epaper -> English Edition (v3)")
    print(f"  PDF:  {args.pdf}")
    print(f"  Date: {args.date or '(auto)'}")
    print("=" * 50)

    start = time.time()

    # ── Step 1: Parse PDF ──
    if not args.skip_parse:
        print(f"\n[1/3] Parsing PDF...")
        result = parse_pdf(args.pdf, args.date, args.dpi)
        date_str = result["date"]
    else:
        # Need to determine the date from args or default
        from datetime import datetime
        date_str = args.date or datetime.now().strftime("%Y-%m-%d")
        print(f"\n[1/3] Skipping parse (using existing pdf_blocks.json for {date_str})")

    # ── Step 2: Translate ──
    if not args.skip_translate:
        print(f"\n[2/3] Translating articles...")
        translate_articles(date_str)
    else:
        print(f"\n[2/3] Skipping translation")
        # Copy pdf_blocks.json -> articles_translated.json with text_en = text_hi
        import json, os
        in_json = os.path.join("data", date_str, "pdf_blocks.json")
        out_json = os.path.join("data", date_str, "articles_translated.json")
        with open(in_json, "r", encoding="utf-8") as f:
            src = json.load(f)
        # Set text_en = text (Hindi) on each block for skip-translate mode
        for page in src.get("pages", []):
            for art in page.get("articles", []):
                for blk in art.get("blocks", []):
                    blk["text_en"] = blk.get("text", "")
        src["source"] = in_json
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(src, f, ensure_ascii=False, indent=2)
        print(f"  Copied as-is -> {out_json}")

    # ── Step 3: Render HTML ──
    print(f"\n[3/3] Rendering HTML...")
    html_path = render_epaper(date_str)

    elapsed = time.time() - start
    print(f"\n{'=' * 50}")
    print(f"  Done in {elapsed:.1f}s!")
    print(f"  Open {html_path} in your browser.")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
