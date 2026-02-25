"""
Step 3: Render translated articles into a standalone HTML file.
"""

import json
import os

from jinja2 import Environment, FileSystemLoader

DATA_DIR = "data"
OUTPUT_DIR = "output"
TEMPLATE_DIR = "templates"


def _paths(date_str: str):
    """Return date-namespaced file paths."""
    data_dir = os.path.join(DATA_DIR, date_str)
    output_dir = os.path.join(OUTPUT_DIR, date_str)
    translated_file = os.path.join(data_dir, "articles_translated.json")
    output_html = os.path.join(output_dir, "index.html")
    return translated_file, output_dir, output_html


def render_html(date_str: str):
    """Load translated data and render to output/{date}/index.html."""

    translated_file, output_dir, output_html = _paths(date_str)

    if not os.path.exists(translated_file):
        print(f"  ERROR: {translated_file} not found. Run translator.py first.")
        return

    with open(translated_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    os.makedirs(output_dir, exist_ok=True)

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
