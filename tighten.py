#!/usr/bin/env python3
"""
tighten.py — remove blank lines *within* each question, keeping a single
blank line only *between* questions.

Standalone helper; it does not touch the other scripts.

A question is a block that begins with "N. " (as produced by format_qa.py),
N running 1, 2, 3, … in order. Every blank / whitespace-only line inside a
block is dropped, so the stem paragraphs and the A/B/C/D choices sit on
consecutive lines; exactly one blank line is inserted before each new
question.

Usage:
    python tighten.py                                 # edits output/aip-c01-merged.txt in place
    python tighten.py <file> [-o out.txt]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_Q_RE = re.compile(r"^(\d+)\. ")


def tighten(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    nextnum = 1
    for line in text.split("\n"):
        line = line.rstrip()
        m = _Q_RE.match(line)
        if m and int(m.group(1)) == nextnum:
            if out:
                out.append("")          # one blank line before each question
            out.append(line)
            nextnum += 1
        elif not line.strip():
            continue                    # drop blank lines inside a question
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Collapse blank lines within questions; keep one between them.")
    ap.add_argument("file", nargs="?", default="output/aip-c01-merged.txt",
                    help="questions file (default: output/aip-c01-merged.txt)")
    ap.add_argument("-o", "--out", default=None,
                    help="output file (default: overwrite the input in place)")
    args = ap.parse_args(argv)

    src = Path(args.file)
    if not src.is_file():
        ap.error(f"not a file: {src}")

    result = tighten(src.read_text(encoding="utf-8"))
    out_path = Path(args.out) if args.out else src
    out_path.write_text(result, encoding="utf-8")

    n = len(re.findall(r"(?m)^\d+\. ", result))
    print(f"tightened {n} questions -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
