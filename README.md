# Aaj Tak Epaper — English Edition

Converts the Aaj Tak Hindi epaper into a polished English version.
The original newspaper layout stays intact (JPG background) with English text overlays.

## Setup

```bash
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
python -m playwright install chromium
```

Add your Grok API key to `.env`:
```
GROK_API_KEY=your_actual_key_here
```

## Usage

```bash
python main.py 2026-02-25
```

Then open `output/index.html` in your browser.

## How It Works

1. **Scraper** — Uses Playwright to load the JS-rendered epaper, extracts article zones and Hindi text
2. **Translator** — Sends Hindi articles to Grok API for polished English translation
3. **Renderer** — Generates HTML with original JPG pages + English overlay boxes at exact coordinates

## Files

| File | Purpose |
|------|---------|
| `scraper.py` | Scrape epaper pages, zones, article text |
| `translator.py` | Translate Hindi → English via Grok |
| `renderer.py` | Generate standalone HTML output |
| `main.py` | Run all 3 steps in sequence |
| `templates/epaper.html.j2` | Jinja2 template for output HTML |
