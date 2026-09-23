"""tests/deny_scan.py -- the privacy gate, as a program.

The policy in CONTRIBUTING.md is enforced here rather than remembered. It runs
in CI over the working tree, the pull request's diff and the commit message,
and it is the same scan that produced this repository's history.

It looks for SHAPES, not for a list of things somebody remembered: an absolute
path, a drive letter, a profile directory, a host name, an e-mail address, an
identifier that could be a real item id or key, a release-name-shaped token.

Status: skeleton -- the rule table lands with the rest of the test suite.
"""
from __future__ import annotations

import argparse
import sys

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tree")
    ap.add_argument("--diff", action="store_true")
    ap.add_argument("--messages", action="store_true")
    ap.parse_args(argv)
    print("deny scan: skeleton, see the module docstring")
    return 0


if __name__ == "__main__":
    sys.exit(main())
