#!/usr/bin/env python3
"""How far two copies of the same modules have drifted apart, line by line.

    python3 tools/drift_report.py OURS THEIRS [--pair OUR_FILE:THEIR_FILE ...]

OURS / THEIRS are directories holding `*.py` files with the same names (for example `world-kernel/worldkernel` here and
the same package in another checkout). Each `--pair` adds two files whose names differ. For every pair it prints the
line counts of both sides and how many lines are identical in the same order; the last line is the total.

Read-only, standard library only, and it prints numbers and file names only (never file contents), so it is safe to
run against a private checkout and paste the table into a public document.
"""
from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path
from typing import List, Tuple


def same_lines(ours: Path, theirs: Path) -> Tuple[int, int, int]:
    a = ours.read_text(encoding="utf-8", errors="replace").splitlines()
    b = theirs.read_text(encoding="utf-8", errors="replace").splitlines()
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return len(a), len(b), sum(block.size for block in matcher.get_matching_blocks())


def collect(ours: Path, theirs: Path) -> Tuple[List[Tuple[str, Path, Path]], List[str], List[str]]:
    mine = {p.name: p for p in sorted(ours.glob("*.py"))}
    other = {p.name: p for p in sorted(theirs.glob("*.py"))}
    both = [(name, mine[name], other[name]) for name in mine if name in other]
    return both, sorted(set(mine) - set(other)), sorted(set(other) - set(mine))


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ours", type=Path)
    ap.add_argument("theirs", type=Path)
    ap.add_argument("--pair", action="append", default=[], metavar="OUR_FILE:THEIR_FILE")
    args = ap.parse_args(argv)

    pairs: List[Tuple[str, Path, Path]] = []
    only_ours: List[str] = []
    only_theirs: List[str] = []
    if args.ours.is_dir() and args.theirs.is_dir():
        pairs, only_ours, only_theirs = collect(args.ours, args.theirs)
    elif args.ours.is_file() and args.theirs.is_file():
        pairs = [(args.ours.name, args.ours, args.theirs)]
    else:
        print("OURS and THEIRS must both be directories or both be files", file=sys.stderr)
        return 2
    for spec in args.pair:
        left, _, right = spec.partition(":")
        if not left or not right or not Path(left).is_file() or not Path(right).is_file():
            print(f"--pair needs two existing files: {spec!r}", file=sys.stderr)
            return 2
        pairs.append((f"{Path(left).name} : {Path(right).name}", Path(left), Path(right)))

    rows = [(name, *same_lines(a, b)) for name, a, b in pairs]
    rows.sort(key=lambda r: -((r[1] - r[3]) + (r[2] - r[3])))
    width = max([len(r[0]) for r in rows] + [5])
    print(f"{'file':<{width}}  {'ours':>6}  {'theirs':>6}  {'same':>6}  {'ours+':>6}  {'theirs+':>7}  {'same/theirs':>11}")
    for name, a, b, s in rows:
        share = f"{100 * s / b:.1f}%" if b else "-"
        print(f"{name:<{width}}  {a:>6}  {b:>6}  {s:>6}  {a - s:>6}  {b - s:>7}  {share:>11}")
    ta, tb, ts = (sum(r[i] for r in rows) for i in (1, 2, 3))
    share = f"{100 * ts / tb:.1f}%" if tb else "-"
    print(f"{'TOTAL (' + str(len(rows)) + ' pairs)':<{width}}  {ta:>6}  {tb:>6}  {ts:>6}  {ta - ts:>6}  {tb - ts:>7}  {share:>11}")
    if only_ours:
        print("only in OURS:", ", ".join(only_ours))
    if only_theirs:
        print("only in THEIRS:", ", ".join(only_theirs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
