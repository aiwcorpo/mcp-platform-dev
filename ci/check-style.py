#!/usr/bin/env python3
"""Style gate: no typographic characters that betray pasted text, no tool traces or placeholder content in the files of the
repository (tracked and new, without ignored ones). Same two patterns as the hand check in the global instructions.
Exit code 1 when anything matches."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHARS = re.compile("[ ​ ﻿‘’“”…]")
PHRASES = re.compile(r"as an ai|generated with|co-authored-by: claude|lorem ipsum|your-api-key|example\.com", re.IGNORECASE)
SELF = {"ci/check-style.py"}


def files():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT, capture_output=True, check=True)
    return [name for name in out.stdout.decode().split("\0") if name and name not in SELF]


def main():
    hits = 0
    for name in files():
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for pattern in (CHARS, PHRASES):
                found = pattern.search(line)
                if found:
                    hits += 1
                    print(f"{name}:{number}: {found.group(0)!r}")
    print(f"check-style: {hits} findings")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
