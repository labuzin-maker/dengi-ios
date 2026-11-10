"""Собирает docs/index.html (панель для хаба и GitHub Pages) из panel.src.html."""
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOCS = HERE.parent / "docs"
HEAD = """<!doctype html>
<html lang="ru">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Датчики">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="theme-color" content="#eaf0f1" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0e1719" media="(prefers-color-scheme: dark)">
<link rel="manifest" href="manifest.webmanifest">
<link rel="apple-touch-icon" href="icon-180.png">
<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>
"""

if __name__ == "__main__":
    DOCS.mkdir(exist_ok=True)
    src = (HERE / "panel.src.html").read_text("utf-8")
    (DOCS / "index.html").write_text(HEAD + src, "utf-8")
    print("docs/index.html собран")
