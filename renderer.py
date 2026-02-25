"""
Step 3: Render translated articles into a standalone HTML file.
"""

import json
import os

from jinja2 import Environment, FileSystemLoader

DATA_DIR = "data"
OUTPUT_DIR = "output"
TRANSLATED_FILE = os.path.join(DATA_DIR, "articles_translated.json")
TEMPLATE_DIR = "templates"
OUTPUT_HTML = os.path.join(OUTPUT_DIR, "index.html")


def render_html(date_str: str):
    """Load translated data and render to output/index.html."""

    if not os.path.exists(TRANSLATED_FILE):
        print(f"  ERROR: {TRANSLATED_FILE} not found. Run translator.py first.")
        return

    with open(TRANSLATED_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    template = env.get_template("epaper.html.j2")

    html = template.render(
        date=date_str,
        pages=data["pages"],
    )

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    total_articles = sum(len(pg["articles"]) for pg in data["pages"])
    print(f"  Done! Generated {OUTPUT_HTML}")
    print(f"  Pages: {len(data['pages'])}, Article overlays: {total_articles}")
    print(f"  Open output/index.html in your browser to view.")


if __name__ == "__main__":
    import sys
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-02-25"
    render_html(date)
