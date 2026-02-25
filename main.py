"""
Aaj Tak Epaper → English Translation Tool
Main orchestrator: scrape → translate → render
"""

import sys
import time

from scraper import scrape_epaper
from translator import translate_articles
from ocr import run_ocr
from renderer import render_html


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-02-25"

    print(f"═══════════════════════════════════════════")
    print(f"  Aaj Tak Epaper → English Edition")
    print(f"  Date: {date}")
    print(f"═══════════════════════════════════════════\n")

    start = time.time()

    print(f"[1/4] Scraping epaper for {date}...")
    scrape_epaper(date)

    print(f"\n[2/4] Translating articles...")
    translate_articles(date)

    print(f"\n[3/4] Running OCR to detect text regions...")
    run_ocr(date)

    print(f"\n[4/4] Rendering HTML...")
    render_html(date)

    elapsed = time.time() - start
    print(f"\n═══════════════════════════════════════════")
    print(f"  All done in {elapsed:.1f}s!")
    print(f"  Open output/{date}/index.html in your browser.")
    print(f"═══════════════════════════════════════════")


if __name__ == "__main__":
    main()
