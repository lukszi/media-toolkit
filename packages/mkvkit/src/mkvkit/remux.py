"""mkvkit.remux -- decide what a rebuild keeps, then build it somewhere else.

Dropping an audio track from a file is a rebuild, and a rebuild is where the
expensive mistakes live: the wrong track goes, the chapters double, the
language subtags change, the output lands on top of the input. So the decision
and the command are separated here. :func:`plan` produces a reviewable object
that names every track and the reason it lives or dies; :func:`command` turns
that into an invocation; :func:`build` runs it into a staging directory.

The rules the planner enforces, none of which are optional:

**Nothing is dropped that was not listed.** A language is droppable only if a
policy says so by name. Everything else is kept, including a language nobody
recognised and a track with no language at all -- an unknown language is not
an empty one.

**The original language is never dropped**, whatever the policy says, because
a file without it is not the work any more. The planner takes the original
language as an argument and refuses to guess it.

**A commentary track is never dropped by language**, because it is in the same
language as something else and it is not a duplicate of it.

**A file never ends up with no audio.** The plan carries a problem instead of
a command if the kept set would not contain at least one policy language or
the original language.

**Chapters are passed with the existing ones suppressed.** Handing the muxer a
chapter document does not replace what is there, it adds an edition -- so a
source that already has marks ends up with two sets. The input option that
suppresses the existing set is written next to the document option here, in
one place, so it cannot be forgotten in the other.

**The source's own subtag convention is kept.** The muxer writes modern
language subtags by default; a source that has none would gain them, which is
a header change nobody asked for in an operation that was supposed to change
only which tracks exist.

**The output is never the input.** A rebuild writes to a staging path, is
verified there, and only then is swapped in -- see :mod:`mkvkit.swap`.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .chapters import xml as chapters_xml
from .config import Config
from .langcodes import canonical
from .probe import MediaProbe, Track, container_mismatch
from .run import Runner, default_runner
from .verify import TracksDropped

__all__ = [
    "COMMENTARY_WORDS",
    "BuildResult",
    "RemuxPlan",
    "RemuxPolicy",
    "TrackDecision",
    "build",
    "command",
    "plan",
    "summarise",
]

log = logging.getLogger(__name__)

#: Words that mark a track as commentary in a track name, in the languages
#: this has been seen in. A commentary track shares a language with the track
#: it comments on and is not a second copy of it.
COMMENTARY_WORDS: Final[tuple[str, ...]] = ("comment", "kommentar", "commentaire")

#: The muxer's exit code for "it worked, with warnings".
_MUX_OK: Final = (0, 1)


@dataclass(frozen=True)
class RemuxPolicy:
    """Which languages may be dropped, and which never may.

    Both lists are configuration. Neither has a default that drops anything:
    a toolkit that removes tracks from somebody's files because a list was
    left empty has chosen the wrong default.
    """

    keep_languages: frozenset[str] = frozenset()
    droppable_languages: frozenset[str] = frozenset()
    keep_commentary: bool = True
    keep_unknown: bool = True
    default_audio: str | None = None

    @classmethod
    def from_config(cls, config: Config) -> RemuxPolicy:
        policy = config.policy
        return cls(
            keep_languages=frozenset(
                c for c in (canonical(x) for x in policy.keep_languages) if c
            ),
            droppable_languages=frozenset(
                c for c in (canonical(x) for x in policy.droppable_languages) if c
            ),
            default_audio=canonical(policy.default_audio),
        )


@dataclass(frozen=True)
class TrackDecision:
    """One track, and the sentence explaining what happens to it."""

    track_id: int
    language: str | None
    keep: bool
    reason: str

    def __str__(self) -> str:
        verb = "keep" if self.keep else "drop"
        return f"track {self.track_id} ({self.language or 'untagged'}): {verb} -- {self.reason}"


@dataclass(frozen=True)
class RemuxPlan:
    """What the rebuild will do, in a form a person can read before it runs."""

    source: Path
    output: Path
    keep_audio: tuple[int, ...] = ()
    drop_audio: tuple[int, ...] = ()
    decisions: tuple[TrackDecision, ...] = ()
    set_default: int | None = None
    chapters: chapters_xml.ChapterSet | None = None
    disable_subtags: bool = False
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def worthwhile(self) -> bool:
        """A rebuild that drops nothing and writes nothing new is not worth doing."""
        return bool(self.drop_audio) or self.chapters is not None

    def expected_delta(self) -> TracksDropped:
        """The declaration the verification will be run against."""
        return TracksDropped(
            dropped=frozenset(self.drop_audio),
            default_moved=self.set_default is not None,
        )

    def __str__(self) -> str:
        lines = [f"{self.source.name} -> {self.output}"]
        lines += [f"  {decision}" for decision in self.decisions]
        lines += [f"  problem: {problem}" for problem in self.problems]
        lines += [f"  note: {note}" for note in self.notes]
        return "\n".join(lines)


def _is_commentary(track: Track) -> bool:
    name = (track.name or "").lower()
    return any(word in name for word in COMMENTARY_WORDS)


def plan(
    found: MediaProbe,
    policy: RemuxPolicy,
    *,
    output: Path | str,
    original_language: str | None,
    chapters: chapters_xml.ChapterSet | None = None,
) -> RemuxPlan:
    """Decide, track by track, with a reason recorded for every one."""
    source = found.path
    target = Path(output)
    problems: list[str] = []
    notes: list[str] = []

    if target.resolve() == source.resolve():
        problems.append(
            "the output is the input; a rebuild is written to a staging path and "
            "swapped in afterwards, never written over the file it is reading"
        )
    mismatch = container_mismatch(found)
    if mismatch is not None:
        notes.append(mismatch)

    original = canonical(original_language)
    if original is None:
        notes.append(
            "the original language is not known, so no track can be dropped for "
            "being a dub of it"
        )

    decisions: list[TrackDecision] = []
    keep: list[int] = []
    drop: list[int] = []
    for track in found.audio:
        language = track.effective_language
        keep_it, reason = _decide(track, language, policy, original)
        decisions.append(TrackDecision(track.id, language, keep_it, reason))
        (keep if keep_it else drop).append(track.id)

    kept_languages = {
        decision.language for decision in decisions if decision.keep
    }
    if not keep:
        problems.append("every audio track would be dropped")
    elif drop and not (
        kept_languages & policy.keep_languages
        or (original is not None and original in kept_languages)
    ):
        problems.append(
            "nothing would be left that a policy language or the original language "
            "names; this is the guard that stops a plan built from a wrong "
            "original language"
        )

    set_default = _default_track(found, policy, keep)
    disable_subtags = all(not track.language_ietf for track in found.tracks)
    if disable_subtags:
        notes.append(
            "the source carries no modern language subtags, so the muxer is told "
            "not to add any"
        )
    if chapters is not None:
        selfcheck = chapters_xml.selfcheck(
            chapters, runtime_s=found.container.duration_s
        )
        problems.extend(
            f"chapters: {problem.rule}: {problem.message}"
            for problem in selfcheck
            if problem.blocking
        )
        notes.extend(
            f"chapters: {problem.rule}: {problem.message}"
            for problem in selfcheck
            if not problem.blocking
        )

    return RemuxPlan(
        source=source,
        output=target,
        keep_audio=tuple(keep),
        drop_audio=tuple(drop),
        decisions=tuple(decisions),
        set_default=set_default,
        chapters=chapters,
        disable_subtags=disable_subtags,
        problems=tuple(problems),
        notes=tuple(notes),
    )


def _decide(
    track: Track,
    language: str | None,
    policy: RemuxPolicy,
    original: str | None,
) -> tuple[bool, str]:
    if language is None:
        return policy.keep_unknown, "no language is claimed for it"
    if original is not None and language == original:
        return True, f"it is the original language ({original})"
    if language in policy.keep_languages:
        return True, f"{language} is kept by policy"
    if language not in policy.droppable_languages:
        return True, f"{language} is not on the droppable list"
    if policy.keep_commentary and _is_commentary(track):
        return True, "its name says it is commentary, not a second copy"
    if original is None:
        return True, "the original language is unknown, so nothing is a dub of it"
    return False, f"{language} is droppable and is not the original language"


def _default_track(
    found: MediaProbe, policy: RemuxPolicy, keep: Sequence[int]
) -> int | None:
    """Which kept track should carry the default flag, if it does not already."""
    if policy.default_audio is None:
        return None
    kept = [track for track in found.audio if track.id in set(keep)]
    if any(track.default and track.effective_language == policy.default_audio
           for track in kept):
        return None
    for track in kept:
        if track.effective_language == policy.default_audio:
            return track.id
    return None


def command(
    remux: RemuxPlan,
    *,
    output: Path | None = None,
    chapters_document: Path | None = None,
) -> list[str]:
    """The muxer invocation, as a list of arguments.

    Written in one place because two of these arguments have to appear
    together or the result is wrong in a way nobody notices until later.
    """
    target = output if output is not None else remux.output
    argv: list[str] = ["-o", str(target)]
    if remux.chapters is not None:
        document = (
            chapters_document
            if chapters_document is not None
            else _chapter_document_path(remux, target)
        )
        argv += ["--chapters", str(document)]
    if remux.disable_subtags:
        argv.append("--disable-language-ietf")
    if remux.keep_audio:
        argv += ["--audio-tracks", ",".join(str(i) for i in remux.keep_audio)]
    if remux.set_default is not None:
        argv += ["--default-track-flag", f"{remux.set_default}:1"]
    if remux.chapters is not None:
        # The document does not replace the file's own marks, it is added
        # beside them -- so the input's chapters are suppressed here. These two
        # arguments belong together and are written together.
        argv.append("--no-chapters")
    argv.append(str(remux.source))
    return argv


def _chapter_document_path(remux: RemuxPlan, target: Path) -> Path:
    return target.with_suffix(target.suffix + ".chapters.xml")


@dataclass(frozen=True)
class BuildResult:
    """What the rebuild did, or would have done."""

    plan: RemuxPlan
    command: tuple[str, ...] = ()
    applied: bool = False
    output: Path | None = None
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.problems


def build(
    remux: RemuxPlan,
    *,
    dry_run: bool = True,
    runner: Runner | None = None,
    config: Config | None = None,
) -> BuildResult:
    """Run the plan into its staging path. Dry run by default.

    The output is written to a part-file and renamed on success, so an
    interrupted run never leaves something that looks like a finished rebuild.
    """
    if not remux.ok:
        return BuildResult(remux, problems=remux.problems)
    run = runner if runner is not None else default_runner(config)
    argv = command(remux)
    if dry_run:
        return BuildResult(
            remux, command=tuple(argv), applied=False,
            notes=("dry run: nothing was written",),
        )

    remux.output.parent.mkdir(parents=True, exist_ok=True)
    part = remux.output.with_suffix(remux.output.suffix + ".part")
    for stale in (part, remux.output):
        if stale.exists():
            stale.unlink()
    document: Path | None = None
    if remux.chapters is not None:
        document = _chapter_document_path(remux, remux.output)
        document.write_text(
            chapters_xml.build(remux.chapters), encoding="utf-8", newline="\n"
        )
    argv = command(remux, output=part, chapters_document=document)
    result = run("mkvmerge", argv, ok=_MUX_OK)
    notes: list[str] = []
    if result.returncode == 1:
        notes.append(f"the muxer warned: {result.tail.splitlines()[-1][:200]}")
    if not part.exists() or part.stat().st_size == 0:
        return BuildResult(
            remux, command=tuple(argv), applied=False,
            problems=("the muxer produced nothing",), notes=tuple(notes),
        )
    part.replace(remux.output)
    if document is not None:
        document.unlink(missing_ok=True)
    return BuildResult(
        remux, command=tuple(argv), applied=True, output=remux.output,
        notes=tuple(notes),
    )


def summarise(plans: Iterable[RemuxPlan]) -> str:
    """A few lines about a batch, for a log or a report."""
    rows = list(plans)
    worthwhile = [p for p in rows if p.worthwhile and p.ok]
    blocked = [p for p in rows if not p.ok]
    dropped = sum(len(p.drop_audio) for p in worthwhile)
    return (
        f"{len(rows)} file(s) considered, {len(worthwhile)} to rebuild "
        f"({dropped} track(s) dropped), {len(blocked)} blocked"
    )
