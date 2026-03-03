Deep Dive — Aaj Tak Epaper → English Edition
=============================================

Overview
--------
This repository converts Aaj Tak Hindi epaper pages into a standalone English "overlay" HTML that preserves the original visual layout (photos, graphics, whitespace) while placing English translations over text regions only.

**Current Version: v3** (modern PDF-based pipeline using PyMuPDF)

v3 Architecture
===============

The v3 pipeline is a complete rewrite focused on **robust article extraction from PDF files** (instead of web scraping) and **block-level, 1:1 translation + rendering** (instead of OCR clustering). It provides:

- **Direct PDF extraction** using PyMuPDF (fitz): extract graphics rectangles, separators, and text blocks from the PDF in a single pass
- **Article boundary detection** via rectangles (hard containers) and horizontal band separators (soft divisions)
- **Column-aware article grouping** to handle multi-column layouts
- **Block-level translation** with role-based classification (headline, subheadline, body, byline, caption)
- **1:1 block overlays** in the rendered HTML (each text block gets its own overlay region with translated text)
- **Background color sampling** from rendered page images so overlays match the original article color

v3 Pipeline (high-level)
------------------------

1. **PDF Parse** (v3/pdf_parser.py)
   - Reads a PDF file and extracts:
     - **Rectangles**: article containers from PDF drawings (width/height > 80px)
     - **Horizontal lines**: band separators (span >= 50% page width)
     - **Text blocks**: via `page.get_text("dict")` with font metadata
   - **Text block role classification**: headline / subheadline / body / byline / caption based on font size, boldness, and position
   - **Block assignment to articles**:
     - **Step 1**: Assign blocks to rectangles (hard containers) if block center falls inside
     - **Step 2**: Remaining blocks assigned to horizontal bands (between separators)
     - **Step 3**: Within bands, detect vertical column boundaries and split into column-based articles
     - **Step 4**: Column groups are merged only if they don't have separate headlines/bylines (avoiding false merges)
   - **Block splitting**: Large blocks with X-gap > 40% page width are split into separate sub-blocks (prevents side-by-side headlines from merging)
   - **Background color sampling**: Each block's background color is sampled from the rendered page image (pixels just outside the block bbox)
   - **Output**: `data/{date}/pdf_blocks.json` with full structure (pages > articles > blocks, each with role, text, coords, bg_color)

2. **Translate Articles** (v3/translator.py)
   - Reads `data/{date}/pdf_blocks.json`
   - **Block-level translation with role preservation**:
     - For each article, builds a role-indexed dict: `{ "headline_0": "...", "body_0": "...", "body_1": "..." }`
     - Sends to LLM with system prompt asking for structured JSON response with same keys
     - Each key is translated independently, preserving role semantics (headlines stay short, body stays paragraph-like)
   - **Batching**: Large articles split into batches of ≤12 blocks to avoid LLM token overflow
   - **Fallback & post-processing**:
     - Empty or still-Hindi values fall back to Hindi text
     - Devanagari character detection (>30% Hindi chars) triggers retry
   - **Output**: `data/{date}/articles_translated.json` with `text_en` field on every block (1:1 mapping)

3. **Render HTML** (v3/renderer.py + templates/epaper_v3.html.j2)
   - Reads `data/{date}/articles_translated.json`
   - Converts PDF coordinates to percentage-based layout (responsive)
   - **1:1 block-level overlays**: Each text block renders its own absolutely-positioned overlay div with:
     - Exact PDF bbox (top_pct, left_pct, width_pct, height_pct)
     - Role-based styling (headline = 28px bold serif, body = 14px serif, etc.)
     - Sampled background color from original article
     - Translated English text (text_en from translator)
   - **Image handling**: Image blocks (no text) are skipped; only text blocks get overlays
   - **Output**: `output/{date}/epaper.html` — single-page HTML with embedded page image and overlays

v3 Key Files & Functions
-------------------------

**v3/pdf_parser.py**

Core functions:

- `_extract_drawings(page)` — Extracts rectangles and lines from PDF drawing objects
- `_filter_container_rects(rects)` — Removes nested/photo rectangles; keeps meaningful article containers
- `_get_band_separators(h_lines, pw)` — Identifies major horizontal band separators
- `_extract_dict_blocks(page, page_idx, pw, ph)` — Extracts text blocks via `get_text("dict")` with:
  - Font size/boldness parsing
  - Decorative span filtering (quotes/dashes don't affect role classification)
  - Line splitting by X-gap for side-by-side content
  - Role classification (_classify_role)
- `_detect_column_boundaries(blocks, pw)` — Detects vertical column gaps from block coverage
- `_split_band_into_articles(blocks, col_boundaries, ...)` — Splits a band's blocks into articles using:
  - Wide vs narrow block classification (wide blocks span columns)
  - Headline/byline anchoring (wide blocks own nearby narrow blocks)
  - Column-aware merging (adjacent columns merge only if both lack anchors)
- `_assign_blocks_to_articles(blocks, container_rects, band_seps, ...)` — Main assignment logic
- `_sample_block_bg_colors(blocks, pix, pw, ph)` — Samples pixel colors outside block edges to determine background
- `parse_pdf(pdf_path, date_str, dpi)` — Entry point; orchestrates full PDF parse

**v3/translator.py**

Core functions:

- `_make_client()` — Creates Groq-compatible OpenAI client (requires GROQ_API_KEY env var)
- `_build_keyed_dict(blocks)` — Builds role-indexed dict from article blocks
- `_parse_json_response(raw)` — Parses JSON from LLM response; handles markdown fences
- `_classify_role(avg_size, max_size, fonts, flags)` — Classifies text block role
- `_is_still_hindi(text)` — Checks if text contains >30% Devanagari characters
- `_translate_article_keyed(client, keyed_dict, retries=3)` — Translates one article's keyed dict via LLM
- `_translate_blocks_batched(client, blocks)` — Translates article blocks, splitting into batches to avoid token overflow
- `translate_articles(date_str)` — Entry point; reads pdf_blocks.json, translates, writes articles_translated.json

**v3/renderer.py**

Core functions:

- `_prepare_render_blocks(pages)` — Extracts render-ready blocks from articles (builds flat list per page)
- `render_epaper(date_str)` — Entry point; reads articles_translated.json, renders epaper.html via Jinja2 template

**templates/epaper_v3.html.j2**

HTML template with:

- Fixed-size page image container (responsive width)
- Absolutely-positioned text-block overlay divs
- Role-based CSS (headline/subheadline/body/byline/caption with different font sizes, weights, colors)
- Solid, opaque backgrounds matching sampled article colors
- No client-side JavaScript for font fitting (overlays use fixed font sizes per role)

**main.py** (entry point)

Orchestrates the full v3 pipeline:

```bash
python main.py                                            # default PDF + today's date
python main.py "path/to/pdf.pdf"                          # custom PDF
python main.py "path/to/pdf.pdf" 2026-03-02              # custom PDF + date
python main.py --skip-parse ...                           # reuse existing pdf_blocks.json
python main.py --skip-translate ...                       # reuse existing articles_translated.json
```

Legacy Pipeline (v1, v2) 
------------------------

*(Deprecated; kept for reference)*

v1 / v2 used web scraping (Playwright), easyocr, and region clustering. For historical context:

1. scrape_epaper (v1/scraper.py)
   - Navigates the epaper with Playwright and downloads page images
   - Extracts article zones (pagerect coordinates: left, top, width, height in a reduced coordinate space)
   - Extracts raw Hindi text for headline/body where possible
   - Saves data to data/{date}/articles_raw.json and articles_translated.json (after translation step)

2. translate_articles (v1/translator.py)
   - Takes raw Hindi article text and produces polished English: headline_en and body_en
   - Uses Groq Cloud API (openai/gpt-oss-20b) with robust retries + caching
   - Writes data/{date}/articles_translated.json

3. OCR (v2/ocr.py)
   - Runs easyocr on the full page images to detect text bounding boxes and OCR text
   - Maps OCR boxes into the article zones (matching by box center-in-zone rule)
   - Produces per-OCR-block metadata (top_pct, left_pct, width_pct, height_pct relative to page; height px; ocr_text; conf)
   - Classifies each OCR block as headline/subheadline/body/byline by heuristic using block height and relative position
   - Outputs data/{date}/articles_ocr.json (same structure as translated file with art.text_blocks added)
   - Incremental mode: saves after every page, resumes from existing file; per-page try/except so one failure doesn't lose progress
   - Handles numpy types for JSON by converting to native Python types / custom encoder
   - GPU: easyocr Reader instantiated with gpu=True when available, otherwise CPU. The pipeline tolerates either.

4. Renderer (v2/renderer.py) + template (templates/epaper.html.j2)
   - Loads articles_ocr.json (prefer OCR) or articles_translated.json (fallback)
   - Converts scraper coordinates into percentage coords used by the template (responsive)
   - Merges OCR blocks into logical overlay regions (not 1:1 OCR block overlays) using a column-aware clustering approach — goal: create overlays that cover text only and avoid covering photos
   - Assigns translated English text into these merged regions (headline_en, body_en distributed proportionally by region area)
   - Renders output/{date}/index.html via Jinja2 template
   - The template positions overlays absolutely over the page image and uses client-side JS to auto-fit fonts (iterative shrink) so English text fits each overlay region

Data Model Summary (v3)
-----------------------

**pdf_blocks.json** (output of v3/pdf_parser.py)

```json
{
  "date": "2026-03-02",
  "source": "d:\\...\\pdf.pdf",
  "pages": [
    {
      "page_num": 1,
      "page_w": 1242,  // PDF width in points
      "page_h": 1754,  // PDF height in points
      "image": "images/page_1.jpg",
      "articles": [
        {
          "article_id": "p1_a1",
          "source": "rect" | "band",  // from rectangle or band separator
          "top": 100.5, "left": 50.2, "width": 400.5, "height": 200.3,  // coords in PDF points
          "top_pct": 5.71, "left_pct": 4.04, "width_pct": 32.2, "height_pct": 11.4,  // percentages
          "block_count": 3,
          "text": "Full Hindi text concatenated from blocks",
          "blocks": [
            {
              "id": "p1_b1",
              "role": "headline",  // headline | subheadline | body | byline | caption
              "text": "मुख्य खबर...",  // Hindi text
              "font_size": 24.5,  // average font size (non-decorative spans only)
              "max_font_size": 28.0,
              "fonts": ["DejaVuSans-Bold"],
              "x0": 50, "y0": 100, "x1": 450, "y1": 135,  // PDF coords
              "top_pct": 5.71, "left_pct": 4.04, "width_pct": 32.2, "height_pct": 2.0,
              "bg_color": "#f5f5dc"  // sampled from rendered page image
            },
            // ... more blocks
          ]
        },
        // ... more articles
      ]
    },
    // ... more pages
  ]
}
```

**articles_translated.json** (output of v3/translator.py, input to v3/renderer.py)

Same structure as pdf_blocks.json, with added `text_en` field on each block:

```json
{
  "blocks": [
    {
      "id": "p1_b1",
      "role": "headline",
      "text": "मुख्य खबर...",  // original Hindi
      "text_en": "Breaking News...",  // translated English
      "font_size": 24.5,
      // ... rest of block data
    }
  ]
}
```

**epaper.html** (output of v3/renderer.py)

Single-page HTML with:
- `<img>` element for page image (1120px responsive)
- Multiple `<div class="text-block">` overlays, each absolutely positioned to cover one text block
- Each overlay has:
  - `class="text-block headline | subheadline | body | byline | caption"`
  - `style="top: X%; left: Y%; width: W%; height: H%; background-color: #...;"`
  - Text content = `text_en` from the block
- Role-based CSS for font size, weight, color, line-height

Why v3 Preserves Photos
------------------------

Unlike the legacy v1/v2 pipelines (which used OCR clustering and thus could accidentally merge blocks across photo gaps), v3:

1. **Extracts exact block regions from PDF**: Each text block has its precise bbox from PyMuPDF, not estimated from OCR clustering
2. **1:1 overlay mapping**: Each PDF block becomes exactly one overlay div; no artificial merging
3. **Respects PDF structure**: Rectangles and band separators are drawn by the PDF layout designer and inherently avoid crossing photos
4. **Background color matching**: Sampled colors ensure overlays blend with the original article, making it obvious where photos are (they won't have sampled background colors)

Result: images stay visible because they're not text blocks and don't get overlays.

How to Run (v3)
---------------

**Requirements**: PyMuPDF, Groq API key for LLM translation

```bash
# Install requirements
pip install -r requirements.txt

# Full pipeline (parse -> translate -> render)
python main.py "path/to/pdf.pdf" 2026-03-02

# Parse only (no translation)
python main.py "path/to/pdf.pdf" 2026-03-02 --skip-translate

# Reuse existing pdf_blocks.json
python main.py "path/to/pdf.pdf" 2026-03-02 --skip-parse

# Help
python main.py --help
```

**Environment variables**

Set `GROQ_API_KEY` for translation:

```bash
export GROQ_API_KEY="your-api-key"
python main.py ...
```

If key not set, translator falls back to Hindi text (text_en = text).

Notes & Tuning
--------------

**Parser tuning** (v3/pdf_parser.py):

- `rw > 80 and rh > 80` — Minimum rectangle size (line 56). Lower for small article boxes.
- `dx > pw * 0.15` and `dy < 3` — Horizontal line detection thresholds (line 69). Adjust for different line weights.
- `line["span"] >= threshold` where `threshold = pw * 0.50` — Band separator span threshold (line 118). Lower to detect more separators; raise to ignore noise.
- `gap_threshold = pw * 0.35` — X-gap threshold for block splitting (line 210). Raise to merge more side-by-side blocks.
- `min_gap=30` in column boundary detection (line 269). Adjust to detect more/fewer columns.

**Translator tuning** (v3/translator.py):

- `MIN_TRANSLATE_CHARS = 15` — Skip translation for articles shorter than this (line 38). Raise for shorter articles.
- `MAX_BLOCKS_PER_BATCH = 12` — Blocks per LLM call (line 41). Lower if LLM drops keys; raise for speed.
- `temperature=0.15` — LLM temperature (line 171). Raise for more creative; lower for consistent.

**Renderer tuning** (v3/renderer.py):

- Template role-based CSS (templates/epaper_v3.html.j2) defines font sizes:
  - Headline: 28px, bold
  - Subheadline: 20px, semi-bold
  - Body: 14px, regular
  - Byline: 12px, italic
  - Caption: 13px, italic
  - Adjust in CSS if text doesn't fit or looks wrong.

Known Limitations & Future Work
-------------------------------

1. **Decorative spans**: Large quote marks or symbols can still influence role classification if they appear as separate spans. The current decorative span filter helps but may need tuning for new layouts.

2. **Column boundary detection**: Based on x-coverage heuristic; fails on very irregular or 1.5-column layouts. Consider ML-based segmentation for complex layouts.

3. **Background color sampling**: Samples pixels just outside block edges; can pick up stray colors or JPEG artifacts. Quantization (8-bit buckets) helps but perfect colors aren't guaranteed.

4. **Translation quality**: Depends entirely on LLM quality and context. Currently translates per-article without full newspaper context. Could improve with multi-article batching.

5. **PDF complexity**: Very old PDFs or PDFs with unusual encoding may have text blocks that don't parse cleanly. Run `_probe_pdf.py` to debug specific PDFs.

Future improvements:

- Add ML model for role classification (headline/body/byline) instead of heuristic font size
- Support multi-page articles with linking
- Add margin/padding tuning per role for better readability
- Implement "review mode" showing original Hindi + English side-by-side for human editing
- Add support for captions / photo credits
- Build a GUI for tuning parser/renderer thresholds interactively
- Add unit tests for core parsing logic

Debugging
---------

**Inspect PDF structure** (ad hoc):

```python
import fitz
doc = fitz.open("path/to/pdf.pdf")
page = doc[0]

# View rectangles
for drw in page.get_drawings():
    for item in drw["items"]:
        if item[0] == "re":
            print(f"Rectangle: {item[1]}")

# View text blocks
d = page.get_text("dict")
for block in d.get("blocks", []):
    if block.get("type") == 0:
        print(f"Text block: {block.get('bbox')}")

doc.close()
```

**Test parser output**:

```bash
python -c "from v3.pdf_parser import parse_pdf; result = parse_pdf('path/to/pdf.pdf', '2026-03-02'); print(result)"
```

**Check translation output**:

```bash
python -c "
import json
with open('data/2026-03-02/articles_translated.json', 'r', encoding='utf-8') as f:
    data = json.load(f)
    for page in data['pages']:
        for art in page['articles']:
            for blk in art['blocks']:
                print(f\"{blk['role']}: {blk.get('text_en', '')[:50]}...\")
"
```

**Open rendered HTML**:

```bash
# On Windows
start output/2026-03-02/epaper.html

# On macOS
open output/2026-03-02/epaper.html

# On Linux
xdg-open output/2026-03-02/epaper.html
```

Summary
-------

The v3 pipeline provides a **robust, PDF-native, block-level translation and rendering system** for converting Hindi epapers to English. Key strengths:

- **Fast & reliable PDF parsing** via PyMuPDF (no OCR, no web scraping)
- **Accurate article boundaries** via rectangles and band separators
- **Smart column detection** for multi-column layouts
- **1:1 block mapping** for perfect rendering fidelity
- **Background color matching** for visual coherence
- **Fallible but effective translation** with role-based batching and retry logic

It's suitable for production use on Aaj Tak epapers and can be adapted to other Hindi newspapers with minor tuning.

