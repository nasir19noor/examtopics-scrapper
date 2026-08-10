#!/usr/bin/env python3
"""
examtopics-scrapper — extract the question + answer options from an
ExamTopics discussion page and save them to a .txt file.

It captures exactly the block the page shows *after* the
"[All ... Questions]" header line and *before* the "by <author> at <date>"
line — i.e. the question text and its A/B/C/D choices, without the page
header, disclaimers, or community comments.

Usage
─────
  python scraper.py <url> [<url> ...]          # one or more URLs
  python scraper.py -f urls.txt                # a file with one URL per line
  python scraper.py <url> -o out --header      # custom out dir, include header

Options
  -o, --out DIR     output directory (default: output)
  -f, --file FILE   read URLs from FILE (one per line; '#' lines ignored)
  --header          prepend the exam name / Question # / Topic # header
  --delay SECONDS   pause between requests (default: 2.0) — be polite
"""

import argparse
import os
import re
import socket
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from requests.adapters import HTTPAdapter

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Per-attempt (connect, read) timeouts.
CONNECT_TIMEOUT = float(os.environ.get("SCRAPER_CONNECT_TIMEOUT", "10"))
READ_TIMEOUT    = float(os.environ.get("SCRAPER_READ_TIMEOUT",    "20"))

# Wall-clock budget for fetch() *including* every retry and backoff. This is
# the number that actually bounds how long a click on "Scrape" can hang:
# per-attempt timeouts alone don't, because retries multiply them.
TOTAL_BUDGET    = float(os.environ.get("SCRAPER_TOTAL_TIMEOUT",   "25"))
MAX_ATTEMPTS    = int(os.environ.get("SCRAPER_MAX_ATTEMPTS",      "3"))
MAX_BACKOFF     = float(os.environ.get("SCRAPER_MAX_BACKOFF",     "4"))

# Successful page HTML is cached for this long, so re-scraping the same URL
# (or two people scraping the same question) costs no round trip at all.
CACHE_TTL       = float(os.environ.get("SCRAPER_CACHE_TTL",       "300"))
CACHE_MAX       = int(os.environ.get("SCRAPER_CACHE_MAX",         "128"))

RETRY_STATUS = frozenset({500, 502, 503, 504})

# In Docker's default bridge network IPv6 is usually unroutable, and
# examtopics.com (Cloudflare) publishes AAAA records — so Python's
# happy-eyeballs path will try IPv6 first and stall at TCP/TLS until the
# kernel SYN timer expires (often >2 min). Force IPv4 resolution by
# default; flip SCRAPER_FORCE_IPV4=0 to disable.
if os.environ.get("SCRAPER_FORCE_IPV4", "1") == "1":
    _orig_getaddrinfo = socket.getaddrinfo
    def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
    socket.getaddrinfo = _ipv4_only_getaddrinfo


def _build_session() -> requests.Session:
    """Session with connection pooling and *no* urllib3-level retries.

    Retries are driven by fetch() instead, so they can be bounded by a single
    wall-clock deadline. urllib3's Retry has no such notion: it honours a
    Retry-After header literally, so three 429s carrying "Retry-After: 60"
    block for a full 180s — longer than gunicorn's worker timeout.
    """
    s = requests.Session()
    adapter = HTTPAdapter(max_retries=0, pool_connections=4, pool_maxsize=8)
    s.mount("https://", adapter)
    s.mount("http://",  adapter)
    return s


_SESSION = _build_session()

# url -> (stored_at, html), guarded because gunicorn runs gthread workers.
_CACHE: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _cache_get(url: str) -> str | None:
    if CACHE_TTL <= 0:
        return None
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(url)
        if hit is None:
            return None
        stored_at, html = hit
        if now - stored_at > CACHE_TTL:
            del _CACHE[url]
            return None
        _CACHE.move_to_end(url)
        return html


def _cache_put(url: str, html: str) -> None:
    if CACHE_TTL <= 0:
        return
    with _CACHE_LOCK:
        _CACHE[url] = (time.monotonic(), html)
        _CACHE.move_to_end(url)
        while len(_CACHE) > CACHE_MAX:
            _CACHE.popitem(last=False)


# ─── HTML → text helpers ───────────────────────────────────────────

def _card_text_to_lines(card: Tag) -> str:
    """Render <p class=card-text> preserving <br> breaks and bullet lines."""
    parts: list[str] = []
    for node in card.children:
        if isinstance(node, NavigableString):
            parts.append(str(node))
        elif isinstance(node, Tag) and node.name == "br":
            parts.append("\n")
        elif isinstance(node, Tag):
            parts.append(node.get_text())
    text = "".join(parts)
    # Normalize: bullet tab -> space, trim each line, collapse 3+ blank lines.
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _extract_choices(container: Tag) -> list[str]:
    """Return choices like 'A. Create an Amazon CloudFront ...'."""
    choices = []
    for li in container.select("li.multi-choice-item"):
        letter_el = li.select_one(".multi-choice-letter")
        letter = (letter_el.get_text(strip=True) if letter_el else "").rstrip(".")
        if letter_el:
            letter_el.extract()  # remove so it isn't duplicated in the body text
        body = re.sub(r"\s+", " ", li.get_text(" ", strip=True)).strip()
        choices.append(f"{letter}. {body}" if letter else body)
    return choices


def _parse_header(header: Tag) -> dict:
    """Pull exam name, question number, and topic number from the header block."""
    raw = header.get_text("\n", strip=True)
    info = {"exam": "", "question": "", "topic": "", "raw": raw}
    m = re.search(r"Question #:\s*(\d+)", raw)
    if m:
        info["question"] = m.group(1)
    m = re.search(r"Topic #:\s*(\d+)", raw)
    if m:
        info["topic"] = m.group(1)
    m = re.search(r"\[All (.+?) Questions\]", raw)
    if m:
        info["exam"] = m.group(1).strip()
    return info


# ─── Core ──────────────────────────────────────────────────────────

class ParseError(Exception):
    pass


class RateLimited(Exception):
    """ExamTopics (Cloudflare) returned 429. Carries the advertised wait."""

    def __init__(self, retry_after: float | None):
        self.retry_after = retry_after
        if retry_after:
            super().__init__(
                f"ExamTopics is rate-limiting this IP. Try again in about "
                f"{int(retry_after)}s."
            )
        else:
            super().__init__("ExamTopics is rate-limiting this IP. Try again shortly.")


class FetchTimeout(Exception):
    pass


def retry_after_seconds(resp: requests.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw.strip())
    except ValueError:
        return None  # HTTP-date form; not worth parsing just to display it


def fetch(url: str, budget: float | None = None, use_cache: bool = True,
          headers: dict | None = None) -> str:
    """GET *url*, retrying transient failures within one wall-clock budget.

    Raises RateLimited immediately on 429 rather than sleeping out the
    Retry-After: a browser request can't usefully wait a minute, and telling
    the user to come back is far better than hanging until the worker dies.
    """
    send = HEADERS if headers is None else headers
    # Authenticated responses differ from anonymous ones, so requests carrying
    # a Cookie must not share the anonymous cache entry (in either direction).
    cache_key = url if "Cookie" not in send else f"{url}\x00{hash(send['Cookie'])}"
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    deadline = time.monotonic() + (TOTAL_BUDGET if budget is None else budget)
    last_error: Exception | None = None

    for attempt in range(MAX_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        try:
            resp = _SESSION.get(
                url,
                headers=send,
                timeout=(min(CONNECT_TIMEOUT, remaining), min(READ_TIMEOUT, remaining)),
            )
        except requests.RequestException as e:
            last_error = e
        else:
            if resp.status_code == 429:
                raise RateLimited(retry_after_seconds(resp))
            if resp.status_code not in RETRY_STATUS:
                resp.raise_for_status()
                if use_cache:
                    _cache_put(cache_key, resp.text)
                return resp.text
            last_error = requests.HTTPError(
                f"{resp.status_code} Server Error for url: {url}", response=resp
            )

        # Exponential backoff, clamped so it can never overrun the deadline.
        if attempt < MAX_ATTEMPTS - 1:
            backoff = min(MAX_BACKOFF, 0.5 * (2 ** attempt))
            if time.monotonic() + backoff >= deadline:
                break
            time.sleep(backoff)

    if last_error is not None:
        if time.monotonic() >= deadline:
            raise FetchTimeout(
                f"Gave up after {TOTAL_BUDGET:.0f}s ({last_error.__class__.__name__}). "
                f"ExamTopics may be slow or blocking this IP."
            ) from last_error
        raise last_error
    raise FetchTimeout(f"Gave up after {TOTAL_BUDGET:.0f}s with no response.")


def parse(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    header_el = soup.select_one("div.question-discussion-header")
    body_el   = soup.select_one("div.question-body")
    if body_el is None:
        raise ParseError("question body not found (page layout changed or not a question page)")

    card = body_el.select_one("p.card-text")
    question_text = _card_text_to_lines(card) if card else ""

    choices_container = soup.select_one("div.question-choices-container")
    choices = _extract_choices(choices_container) if choices_container else []

    info = _parse_header(header_el) if header_el else {"exam": "", "question": "", "topic": ""}

    return {"info": info, "question": question_text, "choices": choices}


def build_text(parsed: dict, include_header: bool) -> str:
    info = parsed["info"]
    blocks = []
    if include_header:
        hdr = []
        if info.get("exam"):
            hdr.append(f"Exam: {info['exam']}")
        if info.get("question"):
            hdr.append(f"Question #: {info['question']}")
        if info.get("topic"):
            hdr.append(f"Topic #: {info['topic']}")
        if hdr:
            blocks.append("\n".join(hdr))
    if parsed["question"]:
        blocks.append(parsed["question"])
    if parsed["choices"]:
        blocks.append("\n".join(parsed["choices"]))
    return "\n\n".join(blocks).strip() + "\n"


def output_name(url: str, info: dict) -> str:
    """Build a stable, descriptive filename."""
    # Exam code like 'SAP-C02' from the exam name, else the URL slug id.
    code = ""
    m = re.search(r"\b([A-Z]{2,4}-?[A-Z]?\d{2,3})\b", info.get("exam", ""))
    if m:
        code = m.group(1)
    qnum = info.get("question", "")
    slug_id = ""
    m = re.search(r"/view/(\d+)", url)
    if m:
        slug_id = m.group(1)

    if code and qnum:
        base = f"{code}_Q{qnum}"
    elif qnum:
        base = f"Q{qnum}_{slug_id}"
    else:
        base = slug_id or "question"
    return re.sub(r"[^\w.-]", "_", base) + ".txt"


def scrape_one(url: str, out_dir: Path, include_header: bool) -> Path:
    parsed = parse(fetch(url))
    text   = build_text(parsed, include_header)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / output_name(url, parsed["info"])
    path.write_text(text, encoding="utf-8")
    return path


# ─── CLI ───────────────────────────────────────────────────────────

def read_url_file(path: str) -> list[str]:
    urls = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scrape ExamTopics question pages to .txt")
    ap.add_argument("urls", nargs="*", help="discussion page URL(s)")
    ap.add_argument("-f", "--file", help="file with one URL per line")
    ap.add_argument("-o", "--out", default="output", help="output directory (default: output)")
    ap.add_argument("--header", action="store_true", help="include exam/question/topic header")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between requests (default: 2.0)")
    args = ap.parse_args(argv)

    urls = list(args.urls)
    if args.file:
        urls += read_url_file(args.file)
    if not urls:
        ap.error("no URLs given (pass URLs as arguments or use -f FILE)")

    out_dir = Path(args.out)
    ok, fail = 0, 0
    for i, url in enumerate(urls):
        try:
            path = scrape_one(url, out_dir, args.header)
            print(f"[OK]   {url}\n       -> {path}")
            ok += 1
        except Exception as e:
            print(f"[FAIL] {url}\n       {type(e).__name__}: {e}", file=sys.stderr)
            fail += 1
        if i < len(urls) - 1 and args.delay > 0:
            time.sleep(args.delay)

    print(f"\nDone: {ok} saved, {fail} failed.")
    return 1 if fail and not ok else 0


if __name__ == "__main__":
    sys.exit(main())
