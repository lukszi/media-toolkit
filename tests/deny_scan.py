"""tests/deny_scan.py -- the privacy gate, as a program.

    python tests/deny_scan.py --tree .
    python tests/deny_scan.py --diff RANGE --messages RANGE
    python tests/deny_scan.py --canary

The policy in CONTRIBUTING.md is enforced here rather than remembered. CI runs
it over the working tree and over every commit a push or a pull request brings,
one at a time, with its message -- and over the whole history once a week --
and the same scan produced this repository's history in the first place.

It looks for **shapes**, not for a list somebody remembered: an absolute path,
a drive letter, a profile directory, a host address, an e-mail address, an
identifier that could be a real item id or key, a release-name-shaped token.
A value nobody thought to list still trips it.

Two things keep it usable.

*The invented cast is masked out first.* The names in `docs/CONVENTIONS.md`
are the only release-shaped, media-filename-shaped strings allowed anywhere,
and they are read from that file rather than repeated here, so adding one to
the cast is a single edit.

*A finding never prints what it matched.* It prints the rule, the file and the
line. A gate that echoes the secret it found has published it into the build
log.

Nothing in this file contains an example of what it looks for: the canary
assembles its planted values at run time, and the rule table is written in
pieces that the language joins and the scanner does not, so the gate never
trips over itself.

**The rule table is written, not typed.** It comes from one table that also
produced this repository's history, and a generator rewrites the block between
the markers below and then checks that every rule here compiles to exactly the
pattern it came from. Editing it by hand is how the two copies drift apart,
which is precisely what a written table is for.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = ["RULES", "Finding", "canary", "main", "scan_text", "scan_tree"]

REPO_ROOT = Path(__file__).resolve().parents[1]
CONVENTIONS = REPO_ROOT / "docs" / "CONVENTIONS.md"

#: Not part of the repository: caches, build output and generated fixtures.
#: Nothing in here is committed, and a tool cache is full of shapes that look
#: like findings and are not.
SKIP_DIRS = {
    ".git", "__pycache__", "_fixtures",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".tox", ".nox",
    ".venv", "venv", "node_modules", "build", "dist", ".eggs",
}


@dataclass(frozen=True)
class Rule:
    id: str
    regex: re.Pattern[str]
    why: str
    allow: tuple[str, ...] = ()


def _rule(id_: str, pattern: str, why: str, allow: Sequence[str] = ()) -> Rule:
    return Rule(id_, re.compile(pattern), why, tuple(allow))


# --- the rule table, written from the private one: do not edit by hand ---
#
# Written from the private shape table by a generator that also checks
# every rule here compiles to exactly the private pattern for the same
# id. Two hand-kept copies of one table drift; this one cannot.
#
# Some patterns are in pieces on purpose. This file is scanned by the
# gate it implements, so a rule that looks for a word must not contain
# that word: the pieces are joined by the language and not by the
# scanner. Nothing here is an example of what it looks for.
RULES: tuple[Rule, ...] = (
    _rule(
        "winpath.drive",
        r'''(?<![A-Za-z0-9])[A-Za-z]:[\\/][A-Za-z0-9_.\-]{2,}|(?<![A-Za-z0-9])[A-Za-z]:\\(?=["'\s)\]]|$)''',
        "a bare drive-letter root leaks the machine's layout",
        ("examples/*", "docs/gotchas/windows-shell.md"),
    ),
    _rule(
        "winpath.userprofile",
        r'''(?i)[A-Za-z]:[\\/]+Users[\\/]+''',
        "a Windows user profile path names the account",
    ),
    _rule(
        "unc",
        '''\\\\\\\\\\'''
        '''?\\\\''',
        "an extended-length UNC prefix only appears in real local paths",
    ),
    _rule(
        "hex32",
        r'''(?<![0-9a-fA-F])[0-9a-f]{32}(?![0-9a-fA-F])''',
        "a 32-hex run is a Jellyfin id, a user id or an API key",
    ),
    _rule(
        "guid.nonfixture",
        r'''(?<![0-9a-fA-F])(?!00000000-0000-0000-0000-)[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}''',
        "any GUID that is not one of the all-zero fixture ids",
    ),
    _rule(
        "secret.assignment",
        r'''(?i)\b(api[_-]?key|apikey|token|secret|password|passwd)\b\s*[:=]\s*["']?[A-Za-z0-9/+_\-]{16,}''',
        "a literal secret assignment, whatever the value is",
    ),
    _rule(
        "email",
        r'''(?i)\b[A-Za-z0-9._%+\-]+@(?!example\.(?:com|org|net)\b)[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b''',
        "any e-mail address other than an example.com one",
    ),
    _rule(
        "host",
        r'''(?i)\b(?!127\.0\.0\.1\b)(?!0\.0\.0\.0\b)\d{1,3}(?:\.\d{1,3}){3}\b''',
        "a bare IP address other than loopback",
        ("docs/gotchas/*",),
    ),
    _rule(
        "release.resolution",
        r'''(?i)\b\d{3,4}p\b[^\n]{0,80}\b(?:BluRay|BDRip|BRRip|WEB-?DL|WEBRip|HDTV|REMUX|HDRip)\b''',
        "a scene-release name is an inventory entry",
    ),
    _rule(
        "release.group",
        r'''(?i)\b[A-Za-z0-9][A-Za-z0-9._]{4,}\.(?:19|20)\d{2}\.[A-Za-z0-9._\-]*\b(?:x26[45]|h26[45]|HEVC|DTS|AAC|AC3|DDP?5)\b''',
        "dotted release-name shape with a codec token",
    ),
    _rule(
        "mediafile",
        r'''(?i)\b[\w'\-]{2,}(?:[ .][\w'\-]+){0,8}\.(?:mkv|mp4|avi|m4v|vob|iso|ts)\b''',
        "a media filename outside the invented cast",
        ("examples/*", "docs/*", "tests/*", "packages/*/tests/*"),
    ),
    _rule(
        "windows.shell",
        r'''(?i)\b(?:robo'''
        r'''copy|MSYS_NO_'''
        r'''PATHCONV|net\s+stop|net\s+start|sc\s+query|scht'''
        r'''asks)\b''',
        "host shell commands belong in examples, not the library",
        ("examples/*", "docs/gotchas/windows-shell.md", "docs/patterns/detached-jobs.md"),
    ),
    _rule(
        "voice.firstperson",
        r'''(?i)\b(?:hous'''
        r'''ehold|my li'''
        r'''brary|our l'''
        r'''ibrary|the f'''
        r'''amily)\b''',
        "first-person references to one specific setup",
    ),
)
# --- end of the written rule table ---


@dataclass(frozen=True)
class Finding:
    rule: str
    where: str
    line: int

    def __str__(self) -> str:  # never prints what it matched
        return f"{self.rule}\t{self.where}:{self.line}"


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


#: A shorter exemption than this is not a name, and masking it would quietly
#: disable every rule that depends on the character it covers.
MIN_EXEMPTION = 4


def cast_exemptions(conventions: Path = CONVENTIONS) -> tuple[str, ...]:
    """The invented cast, read from the one file that defines it."""
    if not conventions.is_file():
        return ()
    quoted = re.findall(r"`([^`\n]+)`", conventions.read_text(encoding="utf-8"))
    names = {_nfc(n) for n in quoted if len(n) >= MIN_EXEMPTION}
    return tuple(sorted(names, key=len, reverse=True))


def _mask(text: str, exemptions: Sequence[str]) -> str:
    """Blank the cast, keeping the length, so reported line numbers stay true."""
    for name in exemptions:
        text = text.replace(name, "·" * len(name))
    return text


def scan_text(
    text: str,
    where: str,
    *,
    path: str | None = None,
    exemptions: Sequence[str] | None = None,
) -> list[Finding]:
    masked = _mask(_nfc(text), cast_exemptions() if exemptions is None else exemptions)
    found: list[Finding] = []
    for rule in RULES:
        if path is not None and any(fnmatch.fnmatchcase(path, a) for a in rule.allow):
            continue
        match = rule.regex.search(masked)
        if match:
            found.append(Finding(rule.id, where, masked.count("\n", 0, match.start()) + 1))
    return found


def scan_tree(root: Path) -> list[Finding]:
    exemptions = cast_exemptions()
    found: list[Finding] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir() or SKIP_DIRS & set(path.parts):
            continue
        data = path.read_bytes()
        if b"\x00" in data[:8192]:
            continue  # binary; the repository is not supposed to contain any
        relative = path.relative_to(root).as_posix()
        found += scan_text(
            data.decode("utf-8", errors="replace"), relative,
            path=relative, exemptions=exemptions,
        )
    return found


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    return completed.stdout if completed.returncode == 0 else ""


def added_lines_by_file(diff: str) -> dict[str, str]:
    """Added lines from a unified diff, kept under the file they were added to.

    Attribution matters: a rule that is allowed in one directory and not in
    another cannot be applied to a diff that has been flattened into one blob.
    Reported line numbers are positions within a file's added lines, not in the
    file itself, which is enough to find the line in a review.
    """
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            current = None if target == "/dev/null" else target.removeprefix("b/")
        elif current and line.startswith("+") and not line.startswith("+++"):
            out.setdefault(current, []).append(line[1:])
    return {path: "\n".join(lines) for path, lines in out.items()}


def scan_range(revisions: str, *, patches: bool, messages: bool) -> list[Finding]:
    exemptions = cast_exemptions()
    found: list[Finding] = []
    if patches:
        for path, added in added_lines_by_file(_git("diff", "--unified=0", revisions)).items():
            found += scan_text(
                added, f"added in {path}", path=path, exemptions=exemptions
            )
    if messages:
        for record in _git("log", "--format=%H%x1f%B%x1e", revisions).split("\x1e"):
            if not record.strip():
                continue
            sha, _, body = record.strip("\n").partition("\x1f")
            found += scan_text(body, f"message {sha[:9]}", exemptions=exemptions)
    return found


# ---------------------------------------------------------------------- canary
def canary() -> int:
    """Plant findings and require them to be caught.

    Each planted value is assembled from pieces at run time, so this file does
    not itself contain the shapes the scanner looks for.
    """
    resolution = "1080" + "p"
    source_tag = "Blu" + "Ray"
    group = "GRO" + "UP"
    four = "4"
    planted = {
        "a profile path": "C" + ":" + "\\" + "Users" + "\\" + "someone" + "\\work",
        "a key": "api_key" + " = " + "x" * 24,
        "an address": "server at " + "198.51." + "100.7",
        "an e-mail": "someone" + "@" + "not-an-example.test",
        "a release name": f"Something.2019.{resolution}.{source_tag}.x26{four}-{group}",
    }
    clean = "a perfectly ordinary line about chapters and languages\n"
    failures = 0
    for description, value in planted.items():
        found = scan_text(f"harmless\n{value}\n", description, exemptions=())
        status = "caught" if found else "MISSED"
        if not found:
            failures += 1
        rules = ",".join(sorted({f.rule for f in found})) or "-"
        print(f"  {status:<6} {description:<16} {rules}")
    if scan_text(clean, "clean line", exemptions=()):
        print("  MISSED clean line flagged")
        failures += 1
    print("canary:", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 0 if failures == 0 else 1


def _report(found: Iterable[Finding]) -> int:
    findings = list(found)
    for finding in findings:
        print(str(finding))
    if findings:
        print(f"deny scan: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="the privacy gate")
    parser.add_argument("--tree", metavar="DIR")
    parser.add_argument("--diff", metavar="RANGE", nargs="?", const="HEAD~1..HEAD")
    parser.add_argument("--messages", metavar="RANGE", nargs="?", const="HEAD~1..HEAD")
    parser.add_argument("--canary", action="store_true")
    args = parser.parse_args(argv)

    if args.canary:
        return canary()
    if not (args.tree or args.diff or args.messages):
        parser.print_help()
        return 2

    found: list[Finding] = []
    if args.tree:
        found += scan_tree(Path(args.tree))
    if args.diff or args.messages:
        revisions = args.diff or args.messages
        if _git("rev-list", "--max-count=1", revisions):
            found += scan_range(
                revisions, patches=bool(args.diff), messages=bool(args.messages)
            )
        else:
            print(f"deny scan: no such range {revisions}, nothing to compare")
    return _report(found)


if __name__ == "__main__":
    sys.exit(main())
