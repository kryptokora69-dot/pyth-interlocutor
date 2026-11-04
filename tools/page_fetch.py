"""Fetch visible text from http(s) URLs. No JS. Cap size."""
from html.parser import HTMLParser
from typing import List
from urllib.parse import urlparse

import requests

ALLOWED = {"http", "https"}
MAX_BYTES = 400_000
MAX_CHARS = 8_000
TIMEOUT = (5, 8)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip:
            return
        t = " ".join(data.split())
        if t:
            self.parts.append(t)


def _ok(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    return p.scheme in ALLOWED and bool(p.netloc)


def fetch_page(url: str) -> dict:
    if not _ok(url):
        return {"url": url, "error": "rejected url"}
    try:
        r = requests.get(
            url,
            timeout=TIMEOUT,
            headers={"User-Agent": "PythResearch/1.0"},
            allow_redirects=True,
        )
        r.raise_for_status()
        raw = r.content[:MAX_BYTES]
        parser = _Text()
        parser.feed(raw.decode(r.encoding or "utf-8", errors="ignore"))
        text = " ".join(parser.parts)[:MAX_CHARS]
        title = ""
        low = text.lower()
        if "bitcoin" in low or "ethereum" in low or "market" in low:
            title = text[:120]
        return {"url": r.url, "status": r.status_code, "text": text, "hint": title}
    except Exception as e:
        return {"url": url, "error": str(e)}


PAGE_DEADLINE = 12   # hard cap per page (connect + slow body), pages fetched in parallel


def fetch_pages(urls) -> dict:
    import threading
    if isinstance(urls, str):
        urls = [urls]
    urls = [u.strip() for u in list(urls or [])[:3] if isinstance(u, str)]
    res = [None] * len(urls)

    def work(i, u):
        res[i] = fetch_page(u)
    ts = [threading.Thread(target=work, args=(i, u), daemon=True) for i, u in enumerate(urls)]
    for t in ts:
        t.start()
    import time
    end = time.time() + PAGE_DEADLINE
    for t in ts:
        t.join(max(0.0, end - time.time()))
    out = [r if r is not None else {"url": u, "error": f"timed out after {PAGE_DEADLINE}s"} for r, u in zip(res, urls)]
    return {"pages": out}
