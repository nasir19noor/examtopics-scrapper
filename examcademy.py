#!/usr/bin/env python3
"""
examcademy.py — pull questions from examcademy.com.

Exam pages look like

    https://examcademy.com/exams/amazon/<exam-slug>/<page>

and hold 25 questions each. The site is a Next.js app: the visible HTML is
mostly skeleton, but the server ships the real content in the RSC "flight"
payload as a series of ``self.__next_f.push([1,"…"])`` calls. Reassembling
those gives, per question, a self-contained MDX chunk:

    <stem markdown>

    <MCQuestion
      choices={{"A":"…","B":"…"}}
      correctAnswer={"B"}
      explanation={"…"}
    />

which is richer than the ExamTopics discussion pages — those give the stem
and choices, but not the answer or an explanation.

Access: only page 1 (questions 1-25) is served anonymously. Pages 2+ render
the app's error component unless you are logged in, so pass a session cookie
with ``--cookie`` to reach them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import scraper

BASE = "https://examcademy.com"

# examcademy signs in through Auth0 with bot protection, and Chrome 151 seals
# its cookies with app-bound encryption — so neither scripted login nor reading
# the browser's cookie store is workable. The one thing that does work is
# lending the tool the session cookie from a logged-in browser, and .env is
# where it lives so it need only be pasted once. See .env.example.
ENV_COOKIE = "EXAMCADEMY_COOKIE"


def load_dotenv(path: str | Path | None = None) -> dict:
    """Minimal KEY=VALUE reader for a .env file. No third-party dependency.

    Values already in the real environment win, so an exported variable can
    override the file. Quotes around a value are stripped; blank lines and
    '#' comments are ignored.
    """
    import os
    if path is None:
        path = Path(__file__).with_name(".env")
    path = Path(path)
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
        os.environ.setdefault(key, val)
    return out


def cookie_from_env() -> str:
    """The saved examcademy cookie, from the real env or .env; '' if unset."""
    import os
    load_dotenv()
    return os.environ.get(ENV_COOKIE, "").strip()

_EXAM_URL_RE = re.compile(
    r"examcademy\.com/exams/([a-z0-9-]+)/([a-z0-9-]+?)(?:/(\d+))?/?$", re.I)

# self.__next_f.push([1,"<js-string>"]) — the payload arrives in many pieces.
_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,\s*("(?:[^"\\]|\\.)*")\]\)')
# A flight chunk definition: "<id>:T<hex-length>,<body>"
_CHUNK_RE = re.compile(r"^([0-9a-f]{1,4}):T([0-9a-f]+),", re.M)
# Which chunk holds which question's MDX.
_QMAP_RE = re.compile(r'"questionNumber":(\d+),"mdxContent":"\$([0-9a-f]+)"')
# The exam's human-readable name, e.g. "AWS Certified … AIP-C01".
_NAME_RE = re.compile(r'"examDisplayName":"([^"]+)"')

QUESTIONS_PER_PAGE = 25


class ExamcademyError(Exception):
    pass


class AccessDenied(ExamcademyError):
    """The page rendered the app's error component — usually login-gated."""


def session_state(html: str) -> str:
    """'authenticated' or 'anonymous', read from the page's own flags.

    The app stamps "hasAccess" on every question widget and renders a Login
    control only for signed-out visitors, so a supplied cookie that comes back
    'anonymous' has expired or was copied for the wrong domain.
    """
    flight = flight_payload(html)
    if '"hasAccess":true' in flight:
        return "authenticated"
    if '"hasAccess":false' in flight:
        return "anonymous"
    return "unknown"


# ─── URL helpers ───────────────────────────────────────────────────

def parse_exam_url(url: str) -> tuple[str, str, int | None]:
    """Split an examcademy exam URL into (provider, exam_slug, page or None)."""
    m = _EXAM_URL_RE.search(url.strip().split("?")[0])
    if not m:
        raise ExamcademyError(
            "Not an examcademy exam URL. Expected something like "
            "https://examcademy.com/exams/amazon/<exam-slug>/1")
    return m.group(1), m.group(2), int(m.group(3)) if m.group(3) else None


def page_url(provider: str, exam: str, page: int) -> str:
    return f"{BASE}/exams/{provider}/{exam}/{page}"


def pages_for(numbers: list[int]) -> list[int]:
    """Which 25-question pages cover these question numbers."""
    return sorted({(n - 1) // QUESTIONS_PER_PAGE + 1 for n in numbers if n > 0})


# ─── Flight payload decoding ───────────────────────────────────────

def flight_payload(html: str) -> str:
    """Reassemble the RSC stream from the inline push() calls."""
    return "".join(json.loads(m.group(1)) for m in _PUSH_RE.finditer(html))


def _chunks(flight: str) -> dict[str, str]:
    """Map chunk id -> chunk body. The T-length is a hex character count."""
    out = {}
    for m in _CHUNK_RE.finditer(flight):
        out[m.group(1)] = flight[m.end():m.end() + int(m.group(2), 16)]
    return out


def _brace_span(text: str, open_at: int) -> str:
    """Return the {...} block starting at open_at, honouring JSON strings."""
    depth, i, in_str, esc = 0, open_at, False, False
    while i < len(text):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_at:i + 1]
        i += 1
    raise ExamcademyError("unbalanced braces in MDX props")


def _jsx_prop(mdx: str, name: str):
    """Read a JSX prop written as name={<json>} (choices uses {{…}})."""
    m = re.search(rf"\b{name}=\{{", mdx)
    if not m:
        return None
    outer = _brace_span(mdx, m.end() - 1)      # includes the JSX braces
    inner = outer[1:-1].strip()                # drop them
    try:
        return json.loads(inner)
    except json.JSONDecodeError:
        return None


def parse_mdx(mdx: str) -> dict:
    """Split one question's MDX into stem, choices, answer and explanation."""
    cut = mdx.find("<MCQuestion")
    stem = (mdx[:cut] if cut >= 0 else mdx).strip()
    props = mdx[cut:] if cut >= 0 else ""
    return {
        "question": re.sub(r"\n{3,}", "\n\n", stem),
        "choices": _jsx_prop(props, "choices") or {},
        "answer": _jsx_prop(props, "correctAnswer") or "",
        "explanation": _jsx_prop(props, "explanation") or "",
    }


def exam_name(html: str) -> str:
    """The exam's display name, or '' if the page doesn't carry one."""
    m = _NAME_RE.search(flight_payload(html))
    return m.group(1) if m else ""


def parse_page(html: str) -> dict[int, dict]:
    """question number -> parsed question, for one exam page."""
    flight = flight_payload(html)
    if not flight:
        raise ExamcademyError("no Next.js payload found (page layout changed?)")

    qmap = dict((int(n), cid) for n, cid in _QMAP_RE.findall(flight))
    if not qmap:
        if "app/exams/error-" in flight:
            raise AccessDenied(
                "examcademy served its error page for this page. Only page 1 "
                "(questions 1-25) is public; sign in and pass --cookie for the rest.")
        raise ExamcademyError("no questions found on this page")

    bodies = _chunks(flight)
    out = {}
    for number, cid in sorted(qmap.items()):
        body = bodies.get(cid)
        if not body:
            continue
        q = parse_mdx(body)
        q["number"] = number
        out[number] = q
    return out


# ─── Rendering ─────────────────────────────────────────────────────

def build_text(q: dict, exam: str = "", include_header: bool = False,
               include_answer: bool = True) -> str:
    """Render one question the same shape scraper.build_text produces."""
    blocks = []
    if include_header:
        hdr = []
        if exam:
            hdr.append(f"Exam: {exam}")
        hdr.append(f"Question #: {q['number']}")
        blocks.append("\n".join(hdr))
    if q["question"]:
        blocks.append(q["question"])
    if q["choices"]:
        blocks.append("\n".join(f"{k}. {v}" for k, v in sorted(q["choices"].items())))
    if include_answer:
        if q.get("answer"):
            blocks.append(f"Correct answer: {q['answer']}")
        if q.get("explanation"):
            blocks.append(f"Explanation:\n{q['explanation']}")
    return "\n\n".join(blocks).strip() + "\n"


# ─── Collection ────────────────────────────────────────────────────

def _gate_hint(cookie: str, state: str) -> str:
    """Say *why* a page was gated, and what to do about it."""
    if not cookie:
        return ("login required. Only page 1 (questions 1-25) is public. Sign in "
                "at examcademy.com (email, Google or Microsoft), then copy the "
                "Cookie request header from DevTools and pass it with --cookie.")
    if state == "anonymous":
        return ("the cookie you supplied was not accepted — the site still sees "
                "an anonymous session. It has probably expired, or was copied "
                "from the wrong domain (it must be examcademy.com, not "
                "auth.examcademy.com). Re-copy it after signing in.")
    return ("signed in, but this page is still gated — the account may not have "
            "access to the full question set for this exam.")


def collect(url: str, numbers: list[int], include_header: bool = False,
            include_answer: bool = True, cookie: str = "") -> tuple[dict[int, str], list[str]]:
    """Fetch the pages covering `numbers`; return {number: text}, [notes]."""
    provider, exam, _ = parse_exam_url(url)
    cookie = cookie or cookie_from_env()
    headers = dict(scraper.HEADERS)
    if cookie:
        headers["Cookie"] = cookie

    wanted = set(numbers)
    results: dict[int, str] = {}
    notes: list[str] = []
    title = ""
    state = ""

    for page in pages_for(numbers):
        target = page_url(provider, exam, page)
        try:
            html = scraper.fetch(target, headers=headers)
            state = state or session_state(html)
            page_qs = parse_page(html)
        except AccessDenied:
            notes.append(f"page {page}: {_gate_hint(cookie, state)}")
            continue
        except Exception as e:                       # noqa: BLE001
            notes.append(f"page {page}: {type(e).__name__}: {e}")
            continue
        title = title or exam_name(html) or exam
        for n, q in page_qs.items():
            if n in wanted:
                results[n] = build_text(q, title, include_header, include_answer)
    return results, notes


# ─── CLI ───────────────────────────────────────────────────────────

def parse_range(spec: str) -> list[int]:
    """Reuse the finder's range syntax: '1-25', '1,3,5-10'."""
    import finder
    return finder.parse_range(spec)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Scrape examcademy.com exam questions into N.txt files.")
    ap.add_argument("url", help="exam URL, e.g. https://examcademy.com/exams/amazon/<slug>/1")
    ap.add_argument("-n", "--numbers", default="1-25",
                    help="question numbers, e.g. '1-25' or '1,3,5-10' (default: 1-25)")
    ap.add_argument("-o", "--out", default="output", help="output directory")
    ap.add_argument("--header", action="store_true",
                    help="prepend exam name / Question #")
    ap.add_argument("--no-answers", action="store_true",
                    help="omit the correct answer and explanation")
    ap.add_argument("--cookie", default="",
                    help="Cookie header from a signed-in browser session, needed "
                         "for pages beyond the first 25 questions")
    ap.add_argument("--check-auth", action="store_true",
                    help="report whether the session is signed in, then exit")
    args = ap.parse_args(argv)

    provider, exam, _ = parse_exam_url(args.url)
    cookie = args.cookie or cookie_from_env()
    headers = dict(scraper.HEADERS)
    if cookie:
        headers["Cookie"] = cookie

    if args.check_auth:
        src = "--cookie" if args.cookie else (".env" if cookie else "none")
        print(f"cookie source: {src}")
        html = scraper.fetch(page_url(provider, exam, 1), headers=headers)
        state = session_state(html)
        print(f"session: {state}")
        if state == "authenticated":
            print("Cookie accepted - gated pages should be reachable.")
            return 0
        print("The site sees an anonymous session, so only questions 1-25 are "
              "reachable.\nSign in at examcademy.com, then copy the whole Cookie "
              "request header\nfor the examcademy.com domain from DevTools "
              "(Network tab -> any document request).")
        return 1

    numbers = parse_range(args.numbers)
    pages = pages_for(numbers)
    print(f"exam  : {exam}\nwant  : {len(numbers)} questions\npages : {pages}")

    results, notes = collect(args.url, numbers, include_header=args.header,
                             include_answer=not args.no_answers, cookie=cookie)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for n in sorted(results):
        (out / f"{n}.txt").write_text(results[n], encoding="utf-8")

    print(f"\nwrote {len(results)} files to {out}/")
    missing = sorted(set(numbers) - set(results))
    if missing:
        print(f"not found: {', '.join(map(str, missing))}")
    for note in notes:
        print(f"  ! {note}")
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
