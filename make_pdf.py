#!/usr/bin/env python3
"""
Render BLOG-DRAFT.md to a print-formatted PDF via headless Chrome.

Handles the markdown subset the draft actually uses: h1/h2, paragraphs, fenced
code, pipe tables, unordered lists, images with captions, bold, inline code,
and em-dash-separated emphasis. Deliberately not a general markdown parser --
it only needs to be right about this one document.

    python3 make_pdf.py
"""
import base64
import html
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "BLOG-DRAFT.md"
OUT_HTML = HERE / "blog-report.html"
OUT_PDF = HERE / "blog-report.pdf"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def inline(s):
    """Inline markdown -> HTML.

    Code spans are swapped for placeholders BEFORE emphasis is applied, then
    restored. Splitting on backticks first (the obvious approach) breaks any
    bold that wraps around inline code -- `**it's a bare `continue`**` gets
    torn across chunks and the ** markers survive into the output.
    """
    stash = []

    def keep(m):
        stash.append(m.group(1))
        return f"\x00{len(stash) - 1}\x00"

    s = re.sub(r"`([^`]+)`", keep, s)
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])", r"<em>\1</em>", s)

    def restore(m):
        return f"<code>{html.escape(stash[int(m.group(1))])}</code>"

    return re.sub(r"\x00(\d+)\x00", restore, s)


def img_data_uri(src):
    p = (HERE / src).resolve()
    if not p.exists():
        return None
    b64 = base64.b64encode(p.read_bytes()).decode()
    return f"data:image/png;base64,{b64}"


def convert(md):
    lines = md.split("\n")
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]

        # fenced code
        if ln.startswith("```"):
            lang = ln[3:].strip()
            i += 1
            buf = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i]); i += 1
            i += 1
            code = html.escape("\n".join(buf))
            cls = f' class="lang-{lang}"' if lang else ""
            out.append(f"<pre{cls}><code>{code}</code></pre>")
            continue

        # image (possibly with a caption wrapped across lines)
        m = re.match(r"!\[(.*)$", ln)
        if m:
            cap = m.group(1)
            while "](" not in cap and i + 1 < len(lines):
                i += 1
                cap += " " + lines[i].strip()
            mm = re.match(r"(.*)\]\((.+?)\)", cap, re.S)
            if mm:
                caption, src = mm.group(1).strip(), mm.group(2)
                uri = img_data_uri(src)
                if uri:
                    out.append(
                        f'<figure><img src="{uri}" alt="">'
                        f"<figcaption>{inline(caption)}</figcaption></figure>")
            i += 1
            continue

        # table
        if ln.strip().startswith("|") and i + 1 < len(lines) and \
                re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            head = [c.strip() for c in ln.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in
                             lines[i].strip().strip("|").split("|")])
                i += 1
            th = "".join(f"<th>{inline(c)}</th>" for c in head)
            tb = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r)
                         + "</tr>" for r in rows)
            out.append(f"<table><thead><tr>{th}</tr></thead>"
                       f"<tbody>{tb}</tbody></table>")
            continue

        # headings
        if ln.startswith("## "):
            out.append(f"<h2>{inline(ln[3:])}</h2>"); i += 1; continue
        if ln.startswith("# "):
            out.append(f"<h1>{inline(ln[2:])}</h1>"); i += 1; continue

        # list
        if ln.startswith("- "):
            items = []
            while i < len(lines) and (lines[i].startswith("- ")
                                      or lines[i].startswith("  ")):
                if lines[i].startswith("- "):
                    items.append(lines[i][2:].strip())
                elif items:
                    items[-1] += " " + lines[i].strip()
                i += 1
            out.append("<ul>" + "".join(f"<li>{inline(x)}</li>"
                                        for x in items) + "</ul>")
            continue

        if ln.strip() == "---":
            i += 1; continue

        # paragraph
        if ln.strip():
            buf = []
            while i < len(lines) and lines[i].strip() and \
                    not lines[i].startswith(("#", "```", "- ", "|", "![")):
                buf.append(lines[i].strip()); i += 1
            out.append(f"<p>{inline(' '.join(buf))}</p>")
            continue
        i += 1
    return "\n".join(out)


CSS = """
@page { size: A4; margin: 18mm 16mm 20mm 16mm; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif;
  font-size: 10.5pt; line-height: 1.62; color: #1a1a1a; margin: 0;
  -webkit-font-smoothing: antialiased;
}
h1 {
  font-size: 23pt; line-height: 1.2; letter-spacing: -0.02em; margin: 0 0 6pt;
  font-weight: 700;
}
h1 + p { font-size: 11.5pt; color: #333; }
h2 {
  font-size: 13.5pt; margin: 22pt 0 7pt; font-weight: 650;
  letter-spacing: -0.01em; padding-bottom: 3pt;
  border-bottom: 0.6pt solid #e2e2e2;
  break-after: avoid; page-break-after: avoid;
}
p { margin: 0 0 9pt; orphans: 3; widows: 3; }
strong { font-weight: 650; }
code {
  font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 8.8pt;
  background: #f4f4f5; padding: 1pt 3pt; border-radius: 3px;
  border: 0.4pt solid #e6e6e8;
}
pre {
  background: #fafafa; border: 0.5pt solid #e4e4e6; border-left: 2.5pt solid #b8b8bd;
  border-radius: 4px; padding: 8pt 10pt; margin: 0 0 11pt; overflow: hidden;
  break-inside: avoid; page-break-inside: avoid;
}
pre code {
  background: none; border: none; padding: 0; font-size: 8.2pt;
  line-height: 1.45; white-space: pre-wrap; word-break: break-word;
}
pre.lang-diff code { font-size: 9pt; }
table {
  border-collapse: collapse; width: 100%; margin: 0 0 12pt; font-size: 9.2pt;
  break-inside: avoid; page-break-inside: avoid;
}
th {
  text-align: left; font-weight: 650; background: #f6f6f7;
  border-bottom: 0.8pt solid #d8d8dc; padding: 5pt 7pt;
}
td { padding: 4.5pt 7pt; border-bottom: 0.4pt solid #ececee; vertical-align: top; }
tbody tr:last-child td { border-bottom: 0.8pt solid #d8d8dc; }
ul { margin: 0 0 10pt; padding-left: 15pt; }
li { margin-bottom: 5pt; }
/* Do NOT use negative margins to widen figures past the text column: Chrome's
   print path clips at the page box rather than bleeding into the @page margin,
   which shears the left edge off every screenshot. The images are ~3300px wide
   rendered into ~178mm, so they are >400dpi in print regardless. */
figure {
  margin: 14pt 0 16pt; break-inside: avoid; page-break-inside: avoid;
  text-align: center;
}
figure img {
  max-width: 100%; height: auto; border: 0.5pt solid #dcdce0; border-radius: 4px;
  display: block; margin: 0 auto;
}
figcaption {
  font-size: 8.4pt; color: #666; margin-top: 5pt; line-height: 1.45;
  text-align: left; padding: 0 2pt;
}
"""


def main():
    if not SRC.exists():
        sys.exit(f"missing {SRC}")
    body = convert(SRC.read_text())
    doc = (f"<!doctype html><html><head><meta charset='utf-8'>"
           f"<title>SigNoz 200 OK</title><style>{CSS}</style></head>"
           f"<body>{body}</body></html>")
    OUT_HTML.write_text(doc)
    print(f"html -> {OUT_HTML}  ({len(doc)//1024} KB)")

    if not os.path.exists(CHROME):
        sys.exit("Chrome not found; open the HTML and print to PDF manually")
    subprocess.run([
        CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
        f"--print-to-pdf={OUT_PDF}", OUT_HTML.as_uri(),
    ], check=True, capture_output=True, timeout=120)
    size = OUT_PDF.stat().st_size // 1024
    print(f"pdf  -> {OUT_PDF}  ({size} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
