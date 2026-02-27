"""
Step 1: Scrape Aaj Tak epaper — extract page images, article zones, and Hindi text.
"""

import json
import os
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


DATA_DIR = "data"
OUTPUT_DIR = "output"


def _paths(date_str: str):
    """Return date-namespaced directories and file paths."""
    data_dir = os.path.join(DATA_DIR, date_str)
    output_dir = os.path.join(OUTPUT_DIR, date_str)
    images_dir = os.path.join(output_dir, "images")
    raw_file = os.path.join(data_dir, "articles_raw.json")
    return data_dir, output_dir, images_dir, raw_file


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


def parse_style(style_str: str) -> dict:
    """Extract top, left, width, height from inline style string."""
    props = {}
    for prop in ["top", "left", "width", "height"]:
        match = re.search(rf"{prop}:\s*([\d.]+)px", style_str)
        if match:
            props[prop] = float(match.group(1))
    return props


def scrape_epaper(date_str: str):
    """
    Main scraping function.
    date_str: 'YYYY-MM-DD' format, e.g. '2026-02-25'
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    ddmmyyyy = dt.strftime("%d%m%Y")
    yyyy_m_d = f"{dt.year}-{dt.month}-{dt.day}"

    data_dir, output_dir, images_dir, raw_file = _paths(date_str)

    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(images_dir, exist_ok=True)

    # ── Step 1: Load the epaper page with Playwright (JS-rendered) ──
    print("  Loading epaper.aajtak.in with headless browser...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1400, "height": 900},
        )
        page = context.new_page()
        page.goto("https://epaper.aajtak.in/", timeout=60000)

        # Wait for the carousel to render
        page.wait_for_selector("#ImageContainer", timeout=30000)
        time.sleep(3)

        # Click through all pages to force lazy-loaded pagerectangles to appear
        print("  Clicking through all carousel pages to load article zones...")
        for pg_click in range(1, 13):
            next_btn = page.query_selector("button.next")
            if next_btn:
                try:
                    next_btn.click(timeout=5000)
                    time.sleep(1.5)
                except Exception:
                    break  # Last page — next button hidden, we're done
            print(f"    Page {pg_click + 1} loaded")

        # Small extra wait for any final DOM updates
        time.sleep(2)

        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "lxml")

    # ── Step 2: Parse the carousel slides ──
    container = soup.find(id="ImageContainer")
    if not container:
        print("  ERROR: Could not find #ImageContainer. Dumping HTML for debug.")
        with open(os.path.join(data_dir, "debug_epaper.html"), "w", encoding="utf-8") as f:
            f.write(html)
        return

    slides = container.find_all("li", class_="mySlides")
    print(f"  Found {len(slides)} page slides")

    pages = []
    seen_storyids = set()  # for deduplication
    article_text_cache = {}  # storyid -> {headline_hi, body_hi}

    for page_idx, slide in enumerate(slides, start=1):
        # ── Get page image URL ──
        img_tag = slide.find("img")
        if not img_tag:
            print(f"  Page {page_idx}: no <img> found, skipping")
            continue

        image_url = img_tag.get("src") or img_tag.get("data-src") or ""
        if not image_url.startswith("http"):
            # Build from pattern
            image_url = (
                f"https://emagazine-static.tosshub.com/epaper-aajtak/alpha/"
                f"epaperimages/{ddmmyyyy}/{ddmmyyyy}-md-hr-{page_idx}.jpg"
            )

        image_local = os.path.join(images_dir, f"page_{page_idx}.jpg")

        # ── Parse pagerectangle article zones ──
        rectangles = slide.find_all("div", class_="pagerectangle")
        articles = []
        zones_kept = []

        for rect in rectangles:
            dpid = rect.get("dpid", "")
            storyid = rect.get("storyid", "")
            pageid = rect.get("pageid", "")
            style = rect.get("style", "")
            coords = parse_style(style)

            if not all(k in coords for k in ("top", "left", "width", "height")):
                continue

            # Check if advertisement
            if "ADVT" in str(dpid).upper():
                zones_kept.append({
                    "type": "advertisement",
                    "top": coords["top"],
                    "left": coords["left"],
                    "width": coords["width"],
                    "height": coords["height"],
                })
                continue

            # Check if video zone (contains SVG)
            if rect.find("svg"):
                zones_kept.append({
                    "type": "video",
                    "top": coords["top"],
                    "left": coords["left"],
                    "width": coords["width"],
                    "height": coords["height"],
                })
                continue

            if not storyid:
                continue

            # Dedup: still record zone coords, but don't re-fetch text
            article_entry = {
                "storyid": storyid,
                "dpid": dpid,
                "pageid": pageid,
                "top": coords["top"],
                "left": coords["left"],
                "width": coords["width"],
                "height": coords["height"],
                "article_url": f"https://epaper.aajtak.in/page1/{dpid}/{storyid}/{yyyy_m_d}",
                "headline_hi": "",
                "body_hi": "",
            }
            articles.append(article_entry)

            if storyid not in seen_storyids:
                seen_storyids.add(storyid)

        pages.append({
            "page_num": page_idx,
            "image_url": image_url,
            "image_local": image_local,
            "articles": articles,
            "zones_kept_as_is": zones_kept,
        })

        print(f"  Page {page_idx}: {len(articles)} articles, {len(zones_kept)} ad/video zones")

    # ── Step 3: Download page images ──
    print("\n  Downloading page images...")
    for pg in pages:
        url = pg["image_url"]
        local = pg["image_local"]
        if os.path.exists(local):
            print(f"    {local} already exists, skipping")
            continue
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 200:
                with open(local, "wb") as f:
                    f.write(resp.content)
                print(f"    Downloaded {local} ({len(resp.content)//1024} KB)")
            else:
                print(f"    WARN: {url} returned {resp.status_code}")
        except Exception as e:
            print(f"    ERROR downloading {url}: {e}")
        time.sleep(0.3)

    # ── Step 4: Fetch article Hindi text ──
    unique_articles = {}
    for pg in pages:
        for art in pg["articles"]:
            sid = art["storyid"]
            if sid not in unique_articles:
                unique_articles[sid] = art

    print(f"\n  Fetching Hindi text for {len(unique_articles)} unique articles...")
    for i, (sid, art) in enumerate(unique_articles.items(), 1):
        url = art["article_url"]
        print(f"    [{i}/{len(unique_articles)}] {url}")
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code == 200:
                art_soup = BeautifulSoup(resp.text, "lxml")
                headline, body = _extract_article_text(art_soup)
                art["headline_hi"] = headline
                art["body_hi"] = body
                article_text_cache[sid] = {"headline_hi": headline, "body_hi": body}
                if headline:
                    print(f"      ✓ headline: {headline[:60]}...")
                else:
                    print(f"      ⚠ no headline found")
            else:
                print(f"      ⚠ HTTP {resp.status_code}")
        except Exception as e:
            print(f"      ✗ Error: {e}")
        time.sleep(0.5)

    # Fill in text for duplicate storyids (zones that appear on multiple pages)
    for pg in pages:
        for art in pg["articles"]:
            sid = art["storyid"]
            if not art["headline_hi"] and sid in article_text_cache:
                art["headline_hi"] = article_text_cache[sid]["headline_hi"]
                art["body_hi"] = article_text_cache[sid]["body_hi"]

    # ── Step 5: Save JSON ──
    output = {
        "date": date_str,
        "pages": pages,
    }
    out_path = os.path.join(data_dir, "articles_raw.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    total_articles = sum(len(pg["articles"]) for pg in pages)
    total_with_text = sum(
        1 for pg in pages for a in pg["articles"] if a["headline_hi"]
    )
    print(f"\n  Done! Saved {out_path}")
    print(f"  Total pages: {len(pages)}")
    print(f"  Total article zones: {total_articles}")
    print(f"  Articles with text: {total_with_text}")


def _extract_article_text(soup: BeautifulSoup) -> tuple:
    """
    Extract headline and body text from an Aaj Tak epaper article page.
    Returns (headline_str, body_str).
    """
    headline = ""
    body = ""

    # Headline: <p class="haedlinesstory"> (note: their typo, not ours)
    el = soup.select_one("p.haedlinesstory")
    if el:
        headline = el.get_text(strip=True)

    # Fallback headline selectors
    if not headline:
        for sel in [".headline_textview", "h1", ".story-headline"]:
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                headline = el.get_text(strip=True)
                break

    # Body: all <p> tags inside <div class="body_text_main">
    body_div = soup.select_one("div.body_text_main")
    if body_div:
        paragraphs = body_div.find_all("p")
        if paragraphs:
            body = "\n\n".join(
                p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True)
            )
        else:
            body = body_div.get_text(strip=True)

    # Fallback body selectors
    if not body:
        for sel in [".mid_content", ".body_content", ".story-content", "article"]:
            el = soup.select_one(sel)
            if el:
                paragraphs = el.find_all("p")
                if paragraphs:
                    body = "\n\n".join(
                        p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True)
                    )
                else:
                    body = el.get_text(strip=True)
                if body:
                    break

    return headline, body


if __name__ == "__main__":
    import sys
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-02-25"
    scrape_epaper(date)
