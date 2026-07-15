#!/usr/bin/env python3
"""
Create the dev.to article from BLOG-DRAFT.md.

Creates it as a DRAFT (published: false). dev.to renders local image paths as
broken images, so ![](images/x.png) is rewritten to raw.githubusercontent.com
URLs from the public repo.

    DEVTO_API_KEY=$(cat .devto-key) python3 post_devto.py           # draft
    DEVTO_API_KEY=$(cat .devto-key) python3 post_devto.py --publish # live
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "BLOG-DRAFT.md"
KEY = os.environ["DEVTO_API_KEY"].strip()
RAW = ("https://raw.githubusercontent.com/KanurkarPrateek/"
       "Signoz-Git-PR-Review-Latency/main/")

TAGS = ["observability", "opentelemetry", "debugging", "devops"]


def build():
    md = SRC.read_text()

    # Title comes from the H1; dev.to owns the title field, so strip it from
    # the body or it renders twice.
    m = re.search(r"^# (.+)$", md, re.M)
    title = m.group(1).strip() if m else "Untitled"
    md = re.sub(r"^# .+\n", "", md, count=1, flags=re.M)

    # Local image paths -> public raw URLs. Captions in this draft wrap across
    # lines, so match with DOTALL and collapse the whitespace.
    def fix(m):
        cap = " ".join(m.group(1).split())
        return f"![{cap}]({RAW}{m.group(2)})"

    md = re.sub(r"!\[(.*?)\]\((images/[^)]+)\)", fix, md, flags=re.S)

    left = re.findall(r"!\[[^\]]*\]\((?!https?://)([^)]+)\)", md)
    if left:
        sys.exit(f"unrewritten local images: {left}")
    return title, md.strip()


def main():
    publish = "--publish" in sys.argv
    title, body = build()
    payload = {"article": {
        "title": title,
        "body_markdown": body,
        "published": publish,
        "tags": TAGS,
    }}
    req = urllib.request.Request(
        "https://dev.to/api/articles", data=json.dumps(payload).encode(),
        method="POST",
        headers={"api-key": KEY, "Content-Type": "application/json",
                 "User-Agent": "signoz-blog"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            d = json.loads(r.read())
        print(f"created (published={d.get('published')})")
        print(f"  title : {d.get('title')}")
        print(f"  url   : {d.get('url')}")
        print(f"  edit  : https://dev.to/{d.get('username','')}/{d.get('slug','')}/edit")
        return 0
    except urllib.error.HTTPError as e:
        print(f"FAILED http={e.code}")
        print(e.read().decode()[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())
