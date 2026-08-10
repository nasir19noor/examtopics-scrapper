#!/usr/bin/env python3
"""
format_qa.py — reshape a merged questions file for study use.

Standalone helper; it does not touch scraper.py / examcademy.py / finder.py /
merge.py.

For each question in the merged file it:
  * folds the bare number line into the first line as "N. <first line>", and
  * drops everything from "Correct answer:" onward (answer + explanation),
    up to the next question.

So a block like

    1
    A company plans to establish …
    …choices…

    Correct answer: B

    Explanation:
    …

becomes

    1. A company plans to establish …
    …choices…

Usage:
    python format_qa.py                                  # edits output/aip-c01-merged.txt in place
    python format_qa.py <file> [-o out.txt]              # custom paths
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_ANSWER_RE = re.compile(r"(?m)^Correct answer:")


def _question_spans(text: str):
    """Yield (number, body) by scanning for bare number lines 1, 2, 3, … in order.

    Scanning sequentially (find '1', then '2' after it, …) means a stray bare
    number inside a choice or explanation can't be mistaken for a marker.
    """
    spans = []
    pos, n = 0, 1
    while True:
        m = re.compile(rf"(?m)^{n}$").search(text, pos)
        if not m:
            break
        spans.append((n, m.start(), m.end()))
        pos, n = m.end(), n + 1

    for i, (num, _start, end) in enumerate(spans):
        stop = spans[i + 1][1] if i + 1 < len(spans) else len(text)
        yield num, text[end:stop]


def reshape(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for num, body in _question_spans(text):
        # Drop the answer + explanation tail.
        cut = _ANSWER_RE.search(body)
        if cut:
            body = body[:cut.start()]
        body = body.strip("\n")
        if not body:
            out.append(f"{num}.")
            continue
        first, _, rest = body.partition("\n")
        block = f"{num}. {first.strip()}"
        if rest.strip("\n"):
            block += "\n" + rest.rstrip()
        out.append(block)
    return "\n\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Number-prefix questions and drop answers/explanations.")
    ap.add_argument("file", nargs="?", default="output/aip-c01-merged.txt",
                    help="merged questions file (default: output/aip-c01-merged.txt)")
    ap.add_argument("-o", "--out", default=None,
                    help="output file (default: overwrite the input in place)")
    args = ap.parse_args(argv)

    src = Path(args.file)
    if not src.is_file():
        ap.error(f"not a file: {src}")

    result = reshape(src.read_text(encoding="utf-8"))
    out_path = Path(args.out) if args.out else src
    out_path.write_text(result, encoding="utf-8")

    n = len(re.findall(r"(?m)^\d+\. ", result))
    print(f"reshaped {n} questions -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
