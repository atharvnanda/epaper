# Epaper Translation Pipeline — Project State (for Copilot / Opus 4.6)

This README is a **full handoff note** for the current main project state.

The goal of this project is to translate Hindi newspaper pages into English while preserving near-100% original layout (multi-column text, spacing, photos, blocks, visual hierarchy).

---

## 1) What changed: V1 → V2

### V1 (legacy approach)
- Used website scraping + OCR on page images.
- Heavy logic around region clustering and OCR block merging.
- Worked but had many brittle points (OCR noise, image-only dependency, complex heuristics).

### V2 (current approach)
- We now use **searchable PDF text directly** (PyMuPDF), not OCR for text extraction.
- PDF pages are still rendered as JPG backgrounds for final overlay.
- We still temporarily scrape website `pagerectangles` (Playwright) for article zone hints.
- Translation/rendering is now moving toward **column-level chunking** (not block-level, not full-article single box).

**Important conceptual shift:**
- Earlier pipeline depended on page images + OCR + website article text pages.
- Current V2 depends on PDF-native text and structure first, with website rectangles only as geometric guides.

---

## 2) Current pipeline files in `v2/`

### `v2/pdf_parser.py`
Input:
- Source searchable PDF file

What it does:
1. Reads PDF blocks using PyMuPDF (`get_text("blocks")`).
2. Renders page JPG backgrounds to `output/{date}/images/page_{n}.jpg`.
3. Scrapes epaper site with Playwright to get `pagerectangles` (`storyid`, `top`, `left`, `width`, `height`).
4. Scales scraped coordinates from fixed space `1128x2050` to actual PDF page size.
5. Assigns each PDF block to a story zone (or `unassigned`) using overlap heuristics.
6. Sorts blocks with column-aware ordering and sets `col_bin`.
7. Splits likely compound article zones into `storyid_part2`, `storyid_part3` by large geometric jumps.
8. Writes `data/{date}/pdf_blocks.json`.

Output shape (high level):
- `pages[]`
  - `articles[]` (`storyid`, `zones[]`, `blocks[]`)
  - `unassigned[]`

### `v2/translator.py`
Input:
- `data/{date}/pdf_blocks.json`

What it does:
1. Groups each article’s blocks by `col_bin`.
2. Merges each column’s coordinates into one column bbox.
3. Joins Hindi text per column (`text_hi`) with paragraph separators.
4. Sends each column chunk to Groq for Hindi→English translation (`text_en`).
5. Writes `data/{date}/articles_translated.json`.

### `v2/renderer.py`
Input:
- `data/{date}/articles_translated.json`

What it does:
1. Renders final `output/{date}/epaper.html` from `templates/epaper.html.j2`.
2. Adds one absolutely-positioned overlay per translated column using exact percentage coordinates.
3. Uses `.pdf-column` overlays and `autoFitText()` in template JS to shrink text into box.

---

## 3) Why we moved to PDF-based extraction

Main reasons:
- OCR introduced noise and unpredictable block boundaries.
- Scraping separate article pages (`headline/body`) from website is not reliable for perfect print layout mapping.
- Searchable PDF already has text blocks and coordinates; this is more deterministic.

So now the strategy is:
- **PDF text as source of truth for text + geometry**,
- Website zones as optional mapping hints,
- Column-level chunking for better translation context and better layout fidelity.

---

## 4) What we changed in `pdf_parser.py` and where it still fails

This is the most important section for Copilot/Opus context.

### Fixes already attempted
1. **Naive y-sort bug fixed**
   - Earlier read top-to-bottom across page, causing cross-column "typewriter" reading.
   - Now blocks are grouped by article and sorted with column binning.

2. **Zone bleed reduced**
   - Earlier center-inside + smallest-area choice caused wrong story assignment.
   - Now assignment uses block-zone overlap percentage with scaled zones.

3. **Global column grid update**
   - `col_bin` now uses page-wide estimated column width (`page_w/50`) so same physical column is binned consistently across different zones.

4. **Compound article splitting heuristic added**
   - One website zone often contains two stories.
   - Split based on sudden vertical gap / width jump, producing `storyid_partN`.

### Known concerns / why it still “doesn’t fully work”
1. **Hard dependency on website rectangles remains**
   - If Playwright scrape fails, dynamic site changes, or page mismatch occurs, mapping quality collapses.

2. **Website zones are semantically weak**
   - `storyid` rectangles are often lazy/merged and not true print-article boundaries.
   - Compound splitting is heuristic and can over-split or miss splits.

3. **Overlap assignment still heuristic**
   - Better than center-point, but adjacent dense layouts can still mis-assign blocks.

4. **Column binning is geometric, not semantic**
   - Pure x/y sorting can still mis-handle irregular wraps, pull quotes, sidebars, and jump lines.

5. **No robust confidence scoring yet**
   - Parser does not output confidence metrics to detect bad pages automatically.

6. **Unassigned blocks still exist**
   - Some content remains unmapped (`unassigned`), requiring fallback handling downstream.

---

## 5) What we are experimenting with now

## Current Experiment: PDF Article Segmentation via Dual-System Localized Geometry

## 1. The Core Objective
We are currently experimenting with a programmatic way to extract text from complex newspaper PDFs so that the text is strictly grouped **article by article** in a natural human reading order. 

Our target output format is a structured text file (like our manually created `test1.txt`) where each article's text blocks `[x0, y0, x1, y1]` are perfectly grouped together and separated by an `xxxxx` delimiter. 

## 2. The Problem We Are Solving
Raw text extraction using PyMuPDF's `get_text("blocks")` completely fails to understand newspaper layouts. It produces raw coordinates like `[x0, y0, x1, y1]`, but suffers from:
* **Headline Detachment:** Massive headlines are extracted out of order and divorced from their body text.
* **Z-Pattern Reading:** It reads across columns indiscriminately, jumping from an article on the bottom-right to one on the top-left.
* **Min-Max Bounding Box Corruption (The Main Blocker):** When we previously tried to group text and calculate a master bounding box (using the `min_x0` and `max_x1` of all text in a group), stray headlines from adjacent columns would accidentally get included. This caused the calculated bounding box to stretch across the entire page, swallowing unrelated articles.

## 3. Our Current Methodology: The "Visual Fence" & Text Binning Strategy
To achieve perfect article separation without bounding box corruption, we are currently experimenting with a **Dual-System Localized Geometry** approach. 

Instead of relying on text coordinates to guess the layout, we use PyMuPDF's vector graphics extractor (`page.get_drawings()`) to physically slice the page into isolated regions using the publisher's own printed lines and boxes.

### Phase 1: Dual-System Fencing
We extract the vector shapes and classify them into two types of "fences":
1.  **Explicit Article Boxes (Localized Regions):** If a graphic is a large drawn rectangle (e.g., `width > 150` and `height > 100`), we treat it as an impenetrable container. Any text inside this box belongs *strictly* to the article inside it.
2.  **Row Dividers (Global Cuts):** If a graphic is a wide, thin line (e.g., `width > 200` and `height < 10`), it acts as a floor or ceiling. These lines slice the remaining "Free Text" (text not inside an explicit box) into horizontal rows.

### Phase 2: Text Binning (The "Drop" Test)
Once the regions (Article Boxes and Free Text Rows) are mathematically defined, we evaluate every raw text block extracted by PyMuPDF.
1.  We calculate the exact center point of the text block: `cx = (x0 + x1) / 2`, `cy = (y0 + y1) / 2`.
2.  We check if that center point falls inside an Explicit Article Box. If yes, it is appended to that box's array.
3.  If it does not fall inside a box, we drop it into the correct Free Text Row based on its Y-coordinate.

### Phase 3: Intra-Region Sorting
Because the text is now safely quarantined inside its correct physical region, it is impossible for an article on the left to bleed into an article on the right. 
Finally, we sort the text *inside* each isolated region:
1.  First by a global column grid (`col_bin = int(x0 / (page_w / 50.0))`) to ensure left-to-right column reading.
2.  Then by `y0` to ensure top-to-bottom reading within that specific column.


---

## 6) Current run order (manual)

```bash
# 1) Parse PDF to article/column-aware blocks
python v2/pdf_parser.py "<path-to-pdf>" --date YYYY-MM-DD

# 2) Translate merged column chunks
python v2/translator.py YYYY-MM-DD

# 3) Render final HTML overlay
python v2/renderer.py YYYY-MM-DD
```

Outputs:
- `data/{date}/pdf_blocks.json`
- `data/{date}/articles_translated.json`
- `output/{date}/epaper.html`

---

## 7) Environment / setup

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

`.env`:
```env
GROQ_API_KEY=your_actual_key_here
```

If `GROQ_API_KEY` is missing, translator currently falls back to Hindi text in `text_en` for testing pipeline flow.

---

## 8) Repo structure (relevant)

- `v1/` legacy pipeline (reference only)
- `v2/pdf_parser.py` current parser with mapping/sorting/splitting heuristics
- `v2/translator.py` column-level merge + translation
- `v2/renderer.py` absolute-position column rendering
- `templates/epaper.html.j2` HTML template with `.pdf-column` overlays
- `data/{date}/...` generated JSON artifacts
- `output/{date}/...` rendered assets and final HTML

---


