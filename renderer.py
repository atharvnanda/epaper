"""
Step 3: Render translated articles into a standalone HTML file.
"""

import json
import os

from jinja2 import Environment, FileSystemLoader

DATA_DIR = "data"
OUTPUT_DIR = "output"
TEMPLATE_DIR = "templates"

# The Playwright scraper captures pagerectangle coordinates in a coordinate
# system that is approximately this wide / tall (derived from the epaper's
# responsive layout at the 1400px viewport the scraper uses).
COORD_SPACE_W = 1128  # approximate max x+width seen across pages
COORD_SPACE_H = 2050  # approximate max y+height seen across pages


def _paths(date_str: str):
    """Return date-namespaced file paths."""
    data_dir = os.path.join(DATA_DIR, date_str)
    output_dir = os.path.join(OUTPUT_DIR, date_str)
    translated_file = os.path.join(data_dir, "articles_translated.json")
    output_html = os.path.join(output_dir, "index.html")
    return translated_file, output_dir, output_html


def _convert_to_pct(pages):
    """Convert absolute px coordinates to percentage-based for responsive overlay."""
    for pg in pages:
        for art in pg.get("articles", []):
            art["top_pct"] = art["top"] / COORD_SPACE_H * 100
            art["left_pct"] = art["left"] / COORD_SPACE_W * 100
            art["width_pct"] = art["width"] / COORD_SPACE_W * 100
            art["height_pct"] = art["height"] / COORD_SPACE_H * 100


def render_html(date_str: str):
    """Load translated data and render to output/{date}/index.html."""

    translated_file, output_dir, output_html = _paths(date_str)

    if not os.path.exists(translated_file):
        print(f"  ERROR: {translated_file} not found. Run translator.py first.")
        return

    with open(translated_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    os.makedirs(output_dir, exist_ok=True)

    # Convert coordinates to percentages for responsive layout
    _convert_to_pct(data["pages"])

    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    template = env.get_template("epaper.html.j2")

    html = template.render(
        date=date_str,
        pages=data["pages"],
    )

    with open(output_html, "w", encoding="utf-8") as f:
        f.write(html)

    total_articles = sum(len(pg["articles"]) for pg in data["pages"])
    print(f"  Done! Generated {output_html}")
    print(f"  Pages: {len(data['pages'])}, Article overlays: {total_articles}")
    print(f"  Open {output_html} in your browser to view.")


if __name__ == "__main__":
    import sys
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-02-25"
    render_html(date)
