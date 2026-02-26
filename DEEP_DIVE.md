Deep Dive — Aaj Tak Epaper → English Edition
=============================================

Overview
--------
This repository converts Aaj Tak Hindi epaper pages into a standalone English "overlay" HTML that preserves the original visual layout (photos, graphics, whitespace) while placing English translations over text regions only.

Pipeline (high-level)
----------------------
1. scrape_epaper (scraper.py)
   - Navigates the epaper with Playwright and downloads page images
   - Extracts article zones (pagerect coordinates: left, top, width, height in a reduced coordinate space)
   - Extracts raw Hindi text for headline/body where possible
   - Saves data to data/{date}/articles_raw.json and articles_translated.json (after translation step)

2. translate_articles (translator.py)
   - Takes raw Hindi article text and produces polished English: headline_en and body_en
   - Uses Groq Cloud API (openai/gpt-oss-20b) with robust retries + caching
   - Writes data/{date}/articles_translated.json

3. OCR (ocr.py)
   - Runs easyocr on the full page images to detect text bounding boxes and OCR text
   - Maps OCR boxes into the article zones (matching by box center-in-zone rule)
   - Produces per-OCR-block metadata (top_pct, left_pct, width_pct, height_pct relative to page; height px; ocr_text; conf)
   - Classifies each OCR block as headline/subheadline/body/byline by heuristic using block height and relative position
   - Outputs data/{date}/articles_ocr.json (same structure as translated file with art.text_blocks added)
   - Incremental mode: saves after every page, resumes from existing file; per-page try/except so one failure doesn't lose progress
   - Handles numpy types for JSON by converting to native Python types / custom encoder
   - GPU: easyocr Reader instantiated with gpu=True when available, otherwise CPU. The pipeline tolerates either.

4. Renderer (renderer.py) + template (templates/epaper.html.j2)
   - Loads articles_ocr.json (prefer OCR) or articles_translated.json (fallback)
   - Converts scraper coordinates into percentage coords used by the template (responsive)
   - Merges OCR blocks into logical overlay regions (not 1:1 OCR block overlays) using a column-aware clustering approach — goal: create overlays that cover text only and avoid covering photos
   - Assigns translated English text into these merged regions (headline_en, body_en distributed proportionally by region area)
   - Renders output/{date}/index.html via Jinja2 template
   - The template positions overlays absolutely over the page image and uses client-side JS to auto-fit fonts (iterative shrink) so English text fits each overlay region

Key files
---------
- scraper.py — scraping + page zone extraction
- translator.py — translation logic + caching
- ocr.py — easyocr integration, mapping OCR boxes to zones, incremental save logic
- renderer.py — region merging, text distribution, final render
- templates/epaper.html.j2 — HTML/CSS/JS that replicates the layout and places overlays
- data/{date}/articles_translated.json — translator output
- data/{date}/articles_ocr.json — translator output enriched with text_blocks from OCR (used by renderer)
- output/{date}/images/page_*.jpg and output/{date}/index.html — rendered results

Coordinate spaces and percentages
---------------------------------
- Scraper extracts article zones in a reduced coordinate space (COORD_SPACE_W x COORD_SPACE_H) — these are absolute pixel-like units relative to the scraper's captured coordinate system.
- Page images are larger actual JPGs. OCR boxes come in image-pixel coordinates.
- OCR maps to page percentages (top_pct, left_pct, width_pct, height_pct) relative to the JPG dimensions so the renderer/template can position overlays responsively.
- Renderer also computes art.top_pct/left_pct/width_pct/height_pct from the zone coordinates using the same COORD_SPACE constants so fallback overlays align.

OCR flow (ocr.py) — important details
------------------------------------
- easyocr.Reader languages: ["hi","en"]. We use gpu=True when available; otherwise cpu-only.
- reader.readtext(img_path) returns [(box, text, conf), ...]; boxes are 4 corner points.
- For each OCR result we create an ocr_box dict converting all numbers to Python float/int to avoid JSON serialization issues.
- Box → percent mapping: top_pct = ob.top / img_h * 100 etc. We also store the pixel height as a font-size proxy.
- Matching OCR boxes to article zones: center-of-box must lie inside zone rectangle (plus margin). This keeps association robust even when OCR boxes cross zone edges.
- Block classification heuristic (_classify_blocks): measured by block pixel height relative to the tallest block in the zone and vertical position. It produces role: headline, subheadline, body, byline.
- Incremental saves: run_ocr() will load an existing articles_ocr.json (resume), skip pages that already have text_blocks on every article, process remaining pages, and save after each page to avoid lost work.

Merging OCR blocks into regions (renderer.py)
-------------------------------------------
Problem: naive merging (single overlay per role) can cover photos that sit between columns. The current approach avoids that by:

1. Grouping OCR blocks by role (headline/subheadline/body/byline).
2. For headlines/subheadlines: spatial clustering (2D adjacency) with looser thresholds because headlines legitimately span horizontally.
3. For body/byline: column-aware clustering that:
   - Estimates column bins from the article zone horizontal span
   - Assigns narrow blocks to the closest column bin; wide blocks are assigned to the bin with most overlap
   - Within each column bin, groups vertically adjacent blocks into clusters using a small vertical gap threshold

Each cluster becomes one overlay region (role, top_pct, left_pct, width_pct, height_pct, avg_block_h, block_count).

This prevents long wide OCR boxes from chaining columns together and preserves photo gaps.

Assigning English text to regions
--------------------------------
- Headline regions: headline_en split across multiple headline regions (if present), or assigned as-is to a single headline region.
- Subheadline regions: filled by the first lines/sentences of the body_en where available.
- Body regions: remaining body_en text is split across body regions proportionally by the region area (width_pct * height_pct).
- Bylines are left empty (we don't automatically translate names/dates to avoid noise).

Template + auto-fit JS
----------------------
- The template places the page image and absolutely positions overlay divs using the percent coordinates.
- Each overlay is given data-text and data-avg-h attributes. No text is written server-side into the divs — the client JS runs autoFitText() which:
  - Estimates starting font size based on role and avg_block_h (OCR-derived height)
  - For body text, computes font size from box area vs character count (heuristic)
  - Iteratively reduces font size until the text fits vertically (limited loops)
  - If text still overflows, clamps the overlay to a vertical clamp style (webkitLineClamp) to avoid overflow
- This approach keeps the rendered layout faithful and responsive and avoids server-side measurement of fonts.

Why this preserves photos
------------------------
- By clustering in 2D and especially by splitting body text into column-based clusters, overlays only cover contiguous text groups.
- Photos that sit between columns produce a vertical and/or horizontal gap that causes separate clusters to be created on either side of the photo rather than one big overlay that covers the photo.

How to run (typical)
--------------------
- Create and activate your venv and install requirements (already done in this workspace).
- Scrape / translate / OCR / render in sequence with main.py:
    python main.py 2026-02-25

- Or run steps individually:
    python scraper.py 2026-02-25
    python translator.py 2026-02-25
    python ocr.py 2026-02-25   # resumes if data/.../articles_ocr.json exists
    python renderer.py 2026-02-25

Notes & troubleshooting
-----------------------
- easyocr GPU: if GPU not available, reader = easyocr.Reader(..., gpu=True) will still work but run on CPU. Expect slower OCR.
- Large OCR runs may take minutes per page on CPU; use incremental mode to avoid losing partial results.
- If you see JSON decoding errors from articles_ocr.json, delete that file and re-run run_ocr (the new code writes atomically via a .tmp then rename).
- If overlays still cover images in certain edge cases, tune clustering thresholds in renderer.py:
  - _column_cluster: est columns heuristic, bin width and gap_y_pct
  - _spatial_cluster: gap_x_pct / gap_y_pct
  - For very irregular layouts you may need to lower gap thresholds so clusters split more aggressively.

Extensibility ideas
-------------------
- Use a small ML model to classify OCR blocks into roles (headline/body) instead of heuristic thresholds.
- Use layout analysis (detect images via edge detection or segmentation) to exclude image regions before clustering.
- Support translation for captions/bylines selectively, with confidence thresholds.
- Add a "review mode" that overlays the original OCR Hindi text and the assigned English text side-by-side for a human editor.

Data model summary (important fields)
-------------------------------------
- data/{date}/articles_translated.json per page/article fields used:
  - page.page_num
  - page.articles[]: article_url, left, top, width, height (scraper-space), headline_en, body_en

- data/{date}/articles_ocr.json (enriched): same as above + per-article
  - art.text_blocks: list of OCR blocks with fields: top_pct,left_pct,width_pct,height_pct,rel_top,height,ocr_text,conf,role
  - art.regions: produced by renderer: regions with role, top_pct,left_pct,width_pct,height_pct,avg_block_h,block_count,en_text

Final notes
-----------
- The design prioritizes visual fidelity: photos/graphics should remain visible, text should be replaced by polished English in-place, and the output file must be a single standalone HTML with images under output/{date}/images.
- The most delicate part is clustering — thresholds were chosen empirically for this epaper. Tune them if you process other newspaper layouts.

If you want, I can:
- Add a CLI to preview region bounding boxes (debug mode) in the browser
- Add a script that visualizes clusters for a page as an SVG for easier tuning
- Add unit tests for the clustering heuristics

