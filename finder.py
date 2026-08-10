#!/usr/bin/env python3
"""
finder.py — locate every discussion page belonging to one exam, by slug.

ExamTopics discussion URLs look like

    /discussions/amazon/view/384242-exam-aws-certified-generative-ai-developer-professional-aip/
                            ^^^^^^ ^-------------------- slug ---------------------------------^
                            id

Two properties of the site make bulk collection possible without a search
engine (which matters: DuckDuckGo is DNS-blocked by some ISPs and Google
serves a consent wall to scripted clients):

1. The slug is ignored on input — ``view/384242-x/`` resolves fine and the
   server answers ``301`` with the *real* slug in ``Location``.
2. That redirect is served to ``HEAD`` with an empty body.

So we can identify the exam a numeric id belongs to for the cost of a tiny
HEAD request, and only download the full page for ids that match the slug
key we want. Ids for one exam are clustered but interleaved with other
exams, so we sweep a bounded window around a seed id rather than assuming a
contiguous run.
"""

from __future__ import annotations

import concurrent.futures as cf
import re
import threading
import time
from dataclasses import dataclass, field

import requests

import scraper
from scraper import HEADERS, RateLimited, retry_after_seconds

BASE = "https://www.examtopics.com"

# A discussion path: capture the provider, the numeric id, and the slug.
# Matches both the URLs users paste and the Location headers we get back.
_URL_RE = re.compile(r"/discussions/([a-z0-9-]+)/view/(\d+)-([a-z0-9-]*?)/?$", re.I)


class FinderError(Exception):
    pass


# ─── URL / range helpers ───────────────────────────────────────────

def parse_seed(url: str) -> tuple[str, int, str]:
    """Split a discussion URL into (provider, id, slug_key)."""
    m = _URL_RE.search(url.strip().split("?")[0])
    if not m:
        raise FinderError(
            "Not an ExamTopics discussion URL. Expected something like "
            "https://www.examtopics.com/discussions/amazon/view/384242-exam-...-aip/"
        )
    provider, ident, slug = m.group(1), int(m.group(2)), m.group(3)
    if not slug:
        raise FinderError("That URL has no slug to match on.")
    return provider, ident, slug


def parse_range(spec: str) -> list[int]:
    """'1-100', '1,3,5-10' -> sorted unique ints. Raises on nonsense."""
    out: set[int] = set()
    for chunk in re.split(r"[,\s]+", spec.strip()):
        if not chunk:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", chunk)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                lo, hi = hi, lo
            if hi - lo > 2000:
                raise FinderError(f"Range '{chunk}' is too large (max 2000).")
            out.update(range(lo, hi + 1))
        elif chunk.isdigit():
            out.add(int(chunk))
        else:
            raise FinderError(f"Don't understand '{chunk}'. Use e.g. 1-100 or 1,3,5-10.")
    if not out:
        raise FinderError("No question numbers given.")
    return sorted(out)


def _spiral(seed: int, window: int, step: int = 1) -> list[int]:
    """Ids to probe, nearest the seed first: seed, seed-1, seed+1, seed-2 …"""
    ids = [seed]
    for d in range(step, window + 1, step):
        if seed - d > 0:
            ids.append(seed - d)
        ids.append(seed + d)
    return ids


# ─── Probing ───────────────────────────────────────────────────────

def probe_slug(session: requests.Session, provider: str, ident: int,
               timeout: tuple[float, float] = (10, 15)) -> str | None:
    """Return the real slug for a discussion id using one bodiless HEAD.

    ``view/<id>-x/`` 301s to the canonical slug, so this costs almost
    nothing compared with downloading the page.
    """
    url = f"{BASE}/discussions/{provider}/view/{ident}-x/"
    r = session.head(url, headers=HEADERS, timeout=timeout, allow_redirects=False)
    if r.status_code == 429:
        raise RateLimited(retry_after_seconds(r))
    if r.status_code in (301, 302, 307, 308):
        m = _URL_RE.search(r.headers.get("Location", "").split("?")[0])
        return m.group(3) if m else None
    return None  # 404 or an id that is already canonical-less


@dataclass
class Progress:
    probed: int = 0
    matched: int = 0
    fetched: int = 0
    total_probe: int = 0
    wanted: int = 0
    phase: str = ""
    done: bool = False
    message: str = ""
    error: str = ""
    results: dict[int, str] = field(default_factory=dict)   # qnum -> text
    ids: dict[int, int] = field(default_factory=dict)       # qnum -> discussion id
    missing: list[int] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "probed": self.probed, "matched": self.matched,
                "fetched": self.fetched, "total_probe": self.total_probe,
                "wanted": self.wanted, "done": self.done, "phase": self.phase,
                "message": self.message, "error": self.error,
                "found": sorted(self.results), "missing": self.missing,
            }


def collect(seed_url: str, numbers: list[int], include_header: bool = False,
            window: int = 400, workers: int = 8, stop: threading.Event | None = None,
            progress: Progress | None = None, extra_seeds: list[str] | None = None,
            reach: int = 30000, coarse_step: int = 6, band_pad: int = 200) -> Progress:
    """Find and scrape every requested question number for one exam.

    An exam's pages are *not* one contiguous run of ids. Questions are added
    in batches over time, and each batch lands wherever the site's id counter
    happens to be, so one exam occupies several narrow bands scattered tens of
    thousands of ids apart. For AIP-C01, questions 1-85 sit at 384102-384293
    but 86-97 sit at 402483-402494 — ~18,200 ids away.

    So this runs in two phases:

    1. **Dense** — sweep every id within ``window`` of each seed. Cheap, and
       usually enough when the wanted numbers are in the seed's own batch.
    2. **Coarse** — if numbers are still missing, probe every ``coarse_step``
       ids outward as far as ``reach``. A batch is dense once you are inside
       it, so a stride smaller than the narrowest band reliably detects one;
       on any hit, the band around it is filled in densely.

    Either phase stops the moment every requested number is in hand.
    """
    provider, seed_id, slug_key = parse_seed(seed_url)
    seeds = [seed_id]
    for extra in extra_seeds or []:
        p, i, _ = parse_seed(extra)
        if p == provider:
            seeds.append(i)

    prog = progress or Progress()
    wanted = set(numbers)
    with prog.lock:
        prog.wanted = len(wanted)
        prog.message = f"Sweeping ids near {seed_id} for '{slug_key}'…"

    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(
        max_retries=0, pool_connections=workers, pool_maxsize=workers * 2))

    remaining = set(wanted)
    hard_stop = threading.Event()
    seen: set[int] = set()
    seen_lock = threading.Lock()

    def halted() -> bool:
        return hard_stop.is_set() or bool(stop and stop.is_set()) or not remaining

    def handle(ident: int) -> bool:
        """Probe one id. Returns True if it belongs to our exam."""
        if halted():
            return False
        with seen_lock:
            if ident in seen:
                return False
            seen.add(ident)
        try:
            slug = probe_slug(session, provider, ident)
        except RateLimited as e:
            hard_stop.set()
            with prog.lock:
                prog.error = str(e)
            return False
        except requests.RequestException:
            slug = None

        with prog.lock:
            prog.probed += 1
        if not slug or slug_key not in slug:
            return False
        with prog.lock:
            prog.matched += 1

        url = f"{BASE}/discussions/{provider}/view/{ident}-{slug}/"
        try:
            parsed = scraper.parse(scraper.fetch(url))
        except RateLimited as e:
            hard_stop.set()
            with prog.lock:
                prog.error = str(e)
            return True
        except Exception:
            return True

        qnum = parsed["info"].get("question")
        if not qnum or not qnum.isdigit():
            return True
        n = int(qnum)
        with prog.lock:
            prog.fetched += 1
            if n in wanted and n not in prog.results:
                prog.results[n] = scraper.build_text(parsed, include_header)
                prog.ids[n] = ident
                prog.message = (f"Found question {n} at id {ident} "
                                f"({len(prog.results)}/{len(wanted)})")
                remaining.discard(n)
        return True

    def sweep(pool, ids: list[int]) -> list[int]:
        """Run a batch of probes, returning the ids that matched the exam."""
        hits = []
        chunk = max(workers * 4, 16)
        for i in range(0, len(ids), chunk):
            if halted():
                break
            batch = ids[i:i + chunk]
            for ident, ok in zip(batch, pool.map(handle, batch)):
                if ok:
                    hits.append(ident)
        return hits

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        # Phase 1 — dense around every seed we were given.
        with prog.lock:
            prog.phase = "dense"
        dense_ids = []
        for s in seeds:
            dense_ids += _spiral(s, window)
        with prog.lock:
            prog.total_probe = len(dense_ids)
        sweep(pool, dense_ids)

        # Phase 2 — coarse hunt for the batches that live somewhere else.
        if not halted():
            with prog.lock:
                prog.phase = "coarse"
                prog.message = (f"{len(prog.results)}/{len(wanted)} found; "
                                f"hunting other id bands…")
            coarse = _spiral(seed_id, reach, coarse_step)
            with prog.lock:
                prog.total_probe += len(coarse)

            chunk = max(workers * 4, 16)
            for i in range(0, len(coarse), chunk):
                if halted():
                    break
                hits = sweep(pool, coarse[i:i + chunk])
                # A hit means we struck a new batch — fill it in densely.
                for h in hits:
                    if halted():
                        break
                    sweep(pool, _spiral(h, band_pad))

    with prog.lock:
        prog.missing = sorted(wanted - set(prog.results))
        prog.phase = "done"
        prog.done = True
        if not prog.error:
            prog.message = (f"Done - {len(prog.results)} of {len(wanted)} questions"
                            + (f", {len(prog.missing)} not found" if prog.missing else ""))
    return prog


# ─── CLI ───────────────────────────────────────────────────────────

def main(argv=None):
    import argparse
    from pathlib import Path

    ap = argparse.ArgumentParser(
        description="Collect many ExamTopics questions from one exam into N.txt files.")
    ap.add_argument("url", help="any one discussion URL from the target exam")
    ap.add_argument("-n", "--numbers", default="1-100",
                    help="question numbers, e.g. '1-100' or '1,3,5-10' (default: 1-100)")
    ap.add_argument("-o", "--out", default="output", help="output directory")
    ap.add_argument("--header", action="store_true",
                    help="include exam/question/topic header in each file")
    ap.add_argument("--window", type=int, default=400,
                    help="ids swept densely either side of each seed (default: 400)")
    ap.add_argument("--workers", type=int, default=8,
                    help="concurrent probes (default: 8)")
    ap.add_argument("--seed", action="append", default=[], metavar="URL",
                    help="extra seed URL from another batch of the same exam; "
                         "repeatable. Skips the slow coarse hunt for that batch.")
    ap.add_argument("--reach", type=int, default=30000,
                    help="how far out the coarse hunt looks (default: 30000)")
    ap.add_argument("--coarse-step", type=int, default=6,
                    help="coarse hunt stride; must be under the narrowest batch "
                         "width (default: 6)")
    args = ap.parse_args(argv)

    numbers = parse_range(args.numbers)
    provider, seed_id, slug_key = parse_seed(args.url)
    print(f"exam slug : {slug_key}\nseed id   : {seed_id}\nwant      : {len(numbers)} questions")

    started = time.time()
    prog = collect(args.url, numbers, include_header=args.header,
                   window=args.window, workers=args.workers,
                   extra_seeds=args.seed, reach=args.reach,
                   coarse_step=args.coarse_step)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for n in sorted(prog.results):
        (out / f"{n}.txt").write_text(prog.results[n], encoding="utf-8")

    print(f"\nswept {prog.probed} ids, {prog.matched} on this exam, "
          f"{time.time() - started:.0f}s")
    print(f"wrote {len(prog.results)} files to {out}/")
    if prog.ids:
        lo, hi = min(prog.ids.values()), max(prog.ids.values())
        print(f"id range used: {lo}..{hi}")
    if prog.missing:
        print(f"not found: {', '.join(map(str, prog.missing))}")
        print("  These have no discussion page, or sit in a batch beyond --reach.\n"
              "  If you know a URL for one of them, pass it with --seed to jump straight there.")
    if prog.error:
        print(f"error: {prog.error}")
        return 1
    return 0 if prog.results else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
