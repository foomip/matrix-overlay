#!/usr/bin/python3
"""Re-wrap debian/changelog bullet blocks to the archive's 80-column habit.

Three releases in a row failed `dpkg-buildpackage` on their own changelog entry,
because a line slipped past 80 characters and the test that guards it (in
tests/test_packaging_assets.py) is right: dpkg/lintian count the bullet text, and
the archive convention is <=80 including the two-space bullet indent. Writing the
entry by hand and hoping is the wrong loop, so this script does the wrapping.

Usage:
    python3 packaging/wrap_changelog.py [debian/changelog] [--check]

`--check` reports whether the file would change (exit 1 if it would) without
writing anything, which is what a CI job or a pre-commit hook wants.

Rules it follows, and the ones it deliberately does not touch:
  * a bullet starts at "<indent>* " and swallows the following lines that are
    indented deeper than the bullet marker;
  * wrapped continuation lines are indented to line up under the bullet text;
  * the target width is 78, leaving headroom under the 80 the test enforces;
  * version headers ("matrix-overlay (0.1.4-2) unstable; ..."), the " -- Name
    <mail>  Tue, ..." trailer lines, blank lines, and any line that is already
    short enough stay exactly as they are. Reproducible output matters more than
    tidiness: running this twice must not change the file again.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

WIDTH = 78
BULLET = re.compile(r"^(\s*)\*\s+(.*)$")
TRAILER = re.compile(r"^ -- ")


def _wrap(text: str, first_prefix: str, cont_prefix: str) -> list[str]:
    words, lines, cur = text.split(), [], first_prefix
    for w in words:
        cand = cur if cur.endswith(" ") else cur + " "
        if len(cand.rstrip()) + 1 + len(w) > WIDTH and cur.strip():
            lines.append(cur.rstrip())
            cur = cont_prefix + w
        else:
            cur = (cur + w) if cur.endswith(" ") else (cur + " " + w).rstrip()
            if cur == first_prefix.rstrip():
                cur = first_prefix + w
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def wrap(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = BULLET.match(line)
        if m and not TRAILER.match(line):
            indent, body = m.group(1), m.group(2)
            cont_indent = " " * (len(indent) + 2)
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if not nxt.strip() or TRAILER.match(nxt) or BULLET.match(nxt):
                    break
                if len(nxt) - len(nxt.lstrip()) <= len(indent):
                    break
                body += " " + nxt.strip()
                j += 1
            out.extend(_wrap(body, indent + "* ", " " * (len(cont_indent))))
            i = j
            continue
        if TRAILER.match(line) or not line.strip() or line.startswith((" ", "\t")):
            out.append(line.rstrip())
            i += 1
            continue
        out.append(line.rstrip())
        i += 1
    trailing_newline = "\n" if text.endswith("\n") else ""
    return "\n".join(out) + trailing_newline


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file", nargs="?", default="debian/changelog")
    ap.add_argument("--check", action="store_true",
                    help="report whether the file needs wrapping; change nothing")
    opts = ap.parse_args(argv)

    path = pathlib.Path(opts.file)
    if not path.is_file():
        print(f"wrap_changelog: no such file: {path}", file=sys.stderr)
        return 2
    original = path.read_text(encoding="utf-8")
    wrapped = wrap(original)
    if wrapped == original:
        print("wrap_changelog: already wrapped")
        return 0
    if opts.check:
        over = [n for n, ln in enumerate(wrapped.splitlines(), 1) if len(ln) > 80]
        print(f"wrap_changelog: {path} needs re-wrapping"
              + (f" (lines still over 80 after wrapping: {over})" if over else ""),
              file=sys.stderr)
        return 1
    path.write_text(wrapped, encoding="utf-8")
    print(f"wrap_changelog: re-wrapped {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
