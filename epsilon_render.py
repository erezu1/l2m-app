#!/usr/bin/env python3
"""epsilon_render: pack one Epsilon document and the viewer into a single HTML file.

    python3 epsilon_convert.py paper.tex                  # once: LaTeX -> paper.l2m/ (the document)
    python3 epsilon_render.py paper.l2m -o paper.html      # any time: the one-file page, no LaTeX

The page carries the document, its drawn formulas (math.json) and the viewer (viewer/theme.json,
theme.css, prefs.js, nav.js, viewer.js) inline, so it works offline and as a Claude artifact. It is
an export: change the theme or the viewer and run this again, the document stays as it is.
"""

import argparse
import base64
import hashlib
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from epsilon_convert import draw_math, load_doc, load_math  # noqa: E402

VIEWER = HERE / "viewer"


def inline_json(obj):
    """JSON that is safe inside <script type="application/json">, in plain ASCII (so the page reads the same
    whatever charset it is served with)."""
    return json.dumps(obj, ensure_ascii=True).replace("</", "<\\/").replace("<!--", "<\\u0021--")


def ascii_html(t):
    return html.escape(t).encode("ascii", "xmlcharrefreplace").decode()


# ---------------------------------------------------------------- the fonts, inside the page
# The page carries its fonts (from Google Fonts, fetched when it is written and kept in ~/.cache/epsilon/fonts): the
# reading font it opens in and the interface font, each whole in weight (variable) for the Latin letters (and the
# extended Latin ones when the paper has them), and for the font list, each font's name in its own face. It then
# asks nothing of the network; only another font the reader picks would be fetched. Without a network when it is
# written, the page fetches its fonts itself, as before.
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
FONT_CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "epsilon" / "fonts"


def _get(url):
    f = FONT_CACHE / hashlib.sha1(url.encode()).hexdigest()
    if f.is_file():
        return f.read_bytes()
    b = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30).read()
    FONT_CACHE.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b)
    return b


def _variable(spec):
    """A family's static weights as one range: 'Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,600;1,...' ->
    'Newsreader:ital,opsz,wght@0,6..72,400..700;1,6..72,400..700'."""
    if "@" not in spec:
        return spec
    fam, rest = spec.split(":", 1)
    axes, tuples = rest.split("@", 1)
    names = axes.split(",")
    if "wght" not in names:
        return spec
    w = names.index("wght")
    groups = {}
    for t in tuples.split(";"):
        v = t.split(",")
        key = tuple(x for i, x in enumerate(v) if i != w)
        groups.setdefault(key, []).append(float(v[w].split("..")[0]))
    out = []
    for key, ws in sorted(groups.items()):
        v = list(key)
        v.insert(w, "%g..%g" % (min(ws), max(ws)) if len(ws) > 1 else "%g" % ws[0])
        out.append(",".join(v))
    return "%s:%s@%s" % (fam, axes, ";".join(out))


def font_css(theme, text):
    """@font-face rules with the fonts inside, and what they cover; ('', None) when they cannot be had."""
    subsets = {"latin"} | ({"latin-ext"} if re.search("[\u0100-\u024f\u1e00-\u1eff]", text) else set())
    fonts = {f["key"]: f for f in theme.get("fonts", [])}
    main = (theme.get("defaults") or {}).get("font")
    specs = ([_variable(fonts[main]["css"])] if main in fonts and fonts[main].get("css") else []) + \
            ([_variable(theme["uiFont"])] if theme.get("uiFont") else [])
    try:
        rules = []
        for spec in specs:
            css = _get("https://fonts.googleapis.com/css2?family=" + spec + "&display=swap").decode()
            for sub, block in re.findall(r"/\* ([\w-]+) \*/\s*(@font-face \{.*?\})", css, re.S):
                if sub in subsets:
                    rules.append(re.sub(r"url\((https://[^)]+)\)", lambda m: "url(data:font/woff2;base64,%s)" % base64.b64encode(_get(m.group(1))).decode(), block))
        for key, f in fonts.items():                     # the names in the font list, each in its own face
            if not f.get("css"):
                continue
            css = _get("https://fonts.googleapis.com/css2?family=" + f["css"].split(":")[0] + "&text=" + urllib.parse.quote(f["name"])).decode()
            m = re.search(r"url\((https://[^)]+)\)", css)
            if m:
                rules.append("@font-face { font-family: 'l2m-pv-%s'; src: url(data:font/woff2;base64,%s) format('woff2'); }" % (
                    key, base64.b64encode(_get(m.group(1))).decode()))
        return "\n".join(rules), {"keys": [main] if main in fonts else [], "ui": bool(theme.get("uiFont")), "previews": True}
    except (OSError, ValueError):
        return "", None


def bundle(doc, cache, out, artifact=False, kicker=None, theme_dir=VIEWER, info=None):
    """Write the one-file page for DOC (images inline) and its drawn formulas CACHE (or None)."""
    theme_dir = Path(theme_dir)
    theme = json.loads((theme_dir / "theme.json").read_text())
    if kicker:
        doc = dict(doc, kicker=kicker)
    read = lambda name: (theme_dir / name).read_text()

    def css():
        # the theme's own images (the glass's soap film) go inside the page, which has no files beside it
        def inline(m):
            f = theme_dir / m.group(1)
            if not f.is_file():
                return m.group(0)
            kind = {".webp": "image/webp", ".png": "image/png", ".svg": "image/svg+xml"}.get(f.suffix, "application/octet-stream")
            return "url(data:%s;base64,%s)" % (kind, base64.b64encode(f.read_bytes()).decode())
        return re.sub(r"url\(([\w.-]+\.(?:webp|png|svg))\)", inline, read("theme.css"))
    faces, inline = font_css(theme, doc.get("body", "") + " " + (doc.get("title") or ""))
    if info and not inline:
        info("fonts not put inside (no network?): the page will fetch them from Google Fonts")
    head = ("<title>%s</title>\n" % ascii_html(doc.get("title") or "Paper") +
            ("<style>\n%s\n</style>\n<script>window.L2M_FONTS_INLINE = %s;</script>\n" % (faces, json.dumps(inline)) if inline else
             '<link rel="preconnect" href="https://fonts.googleapis.com">\n<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n') +
            "<style>\n%s</style>\n<script>window.L2M_THEME = %s;</script>\n<script>\n%s</script>\n" % (
        css(), inline_json(theme), read("prefs.js")))
    boot = ('(function () {\n'
            '  var doc = JSON.parse(document.getElementById("l2m-doc").textContent);\n'
            '  var cache = JSON.parse(document.getElementById("l2m-math").textContent);\n'
            '  L2M_open({doc: doc, theme: window.L2M_THEME, cache: cache, key: doc.source || ""});\n'
            '})();\n')
    body = ('<main><noscript>This page needs JavaScript to show the paper.</noscript></main>\n'
            '<script type="application/json" id="l2m-doc">%s</script>\n'
            '<script type="application/json" id="l2m-math">%s</script>\n'
            "<script>\n%s</script>\n<script>\n%s</script>\n<script>\n%s</script>\n") % (
        inline_json(doc), inline_json(cache), read("nav.js"), read("viewer.js"), boot)
    if artifact:
        page = head + body
    else:
        page = ('<!doctype html>\n<html lang="%s">\n<head>\n<meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
                '%s</head>\n<body>\n%s</body>\n</html>\n') % (html.escape(doc.get("lang") or "en"), head, body)
    Path(out).write_text(page)
    if info:
        info("wrote %s (%.1f MB)" % (out, len(page.encode()) / 1e6))


def main():
    ap = argparse.ArgumentParser(description="Pack an Epsilon document and the viewer into one HTML file.")
    ap.add_argument("doc", help="the document folder written by epsilon_convert.py (or its paper.json)")
    ap.add_argument("-o", "--output", help="output .html file (default: next to the document folder)")
    ap.add_argument("--artifact", action="store_true",
                    help="write a page fragment without <html>/<head>/<body>, for publishing as a Claude artifact")
    ap.add_argument("--kicker", help="small line above the title (default: the one given when converting)")
    ap.add_argument("--theme", metavar="DIR", default=str(VIEWER), help="the viewer folder (default: viewer/)")
    ap.add_argument("-q", "--quiet", action="store_true", help="print nothing but errors")
    a = ap.parse_args()
    path = Path(a.doc)
    folder = path if path.is_dir() else path.parent
    out = Path(a.output) if a.output else folder.with_suffix(".html")
    info = None if a.quiet else (lambda m: print(m, file=sys.stderr))
    doc = load_doc(path)
    cache = load_math(folder, doc)
    if cache is None and doc["math"]["items"]:
        warnings = {}
        cache = draw_math(doc, lambda m: warnings.__setitem__(m, warnings.get(m, 0) + 1), info)
        if warnings and not a.quiet:
            print("%d formula warnings (math.json was missing or out of date)" % sum(warnings.values()), file=sys.stderr)
    bundle(doc, cache, out, a.artifact, a.kicker, a.theme, info)


if __name__ == "__main__":
    main()
