#!/usr/bin/env python3
"""
merge.py — merge the per-question files in a directory into one file.

Standalone helper; it does not touch scraper.py / examcademy.py / finder.py.

Each source file looks like:

    Exam: AWS Certified Generative AI Developer - Professional AIP-C01
    Question #: 2

    <question text and choices…>

This drops the "Exam:" line and reduces "Question #: 2" to just "2", then
concatenates every N.txt in numeric order into a single output file.

Usage:
    python merge.py                       # output/aip-c01 -> output/aip-c01-merged.txt
    python merge.py <dir> [-o out.txt]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_EXAM_RE = re.compile(r"^Exam:.*\r?\n?", re.M)
_QNUM_RE = re.compile(r"^Question #:\s*(\d+)\s*\r?\n?", re.M)


def strip_header(text: str, number: int) -> str:
    """Remove the 'Exam:' line and turn 'Question #: N' into a bare 'N'."""
    text = _EXAM_RE.sub("", text, count=1)
    text, n = _QNUM_RE.subn(lambda m: f"{m.group(1)}\n", text, count=1)
    if n == 0:                       # no header present: prepend the file number
        text = f"{number}\n\n{text.lstrip()}"
    return text.strip("\r\n")


def numbered_files(src: Path) -> list[tuple[int, Path]]:
    """All <int>.txt files, sorted by their numeric name."""
    out = []
    for f in src.glob("*.txt"):
        if f.stem.isdigit():
            out.append((int(f.stem), f))
    return sorted(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Merge per-question .txt files into one.")
    ap.add_argument("dir", nargs="?", default="output/aip-c01",
                    help="directory of N.txt files (default: output/aip-c01)")
    ap.add_argument("-o", "--out", default=None,
                    help="output file (default: <dir>-merged.txt)")
    ap.add_argument("--sep", default="\n\n\n",
                    help="separator between questions (default: two blank lines)")
    args = ap.parse_args(argv)

    src = Path(args.dir)
    if not src.is_dir():
        ap.error(f"not a directory: {src}")

    files = numbered_files(src)
    if not files:
        ap.error(f"no N.txt files found in {src}")

    out_path = Path(args.out) if args.out else src.with_name(src.name + "-merged.txt")

    blocks = [strip_header(f.read_text(encoding="utf-8"), n) for n, f in files]
    out_path.write_text(args.sep.join(blocks) + "\n", encoding="utf-8")

    print(f"merged {len(files)} files ({files[0][0]}-{files[-1][0]}) -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
