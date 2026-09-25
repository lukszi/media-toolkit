"""mkvkit.propedit -- one in-place edit, proved to have changed only what it claimed.

The header editor rewrites a file's headers without touching a packet. That
makes it the right tool for a language, a flag, a chapter set or a tag -- and
a dangerous one, because it is fast, it is quiet, and "it exited zero" is not
evidence of anything.

What this module adds around it:

**A container guard.** The editor writes Matroska and nothing else. Handed a
file with a Matroska name and another container inside, it does nothing at all
and exits zero. Such a file is refused here and routed to a rebuild.

**Selection by the track's own identifier.** The editor can select a track by
position among its type -- the second audio track -- which is the selector
every hand-written pass reaches for and the one that edits the wrong track as
soon as a file's tracks are not in the order you assumed. Edits here name the
unique identifier the track carries, which cannot drift.

**A language edit is refused when a tag would overrule it.** Setting the
header of a file whose tag element claims something else produces a file that
reads the old way everywhere that matters, and a track table that says
otherwise. Either the tag is rewritten in the same call or the edit does not
happen.

**A comparison that is keyed on the identifier and blind to normalisation.**
Before and after are read with the same reader, matched track by track on the
identifier, and compared field by field. Tags are compared as triples, because
the editor re-emits the target block in its own normal form and a
whole-element comparison would report every tag as lost and re-added on every
single run -- which is precisely where a real loss would hide.

**A rollback artefact, always.** Generated before the edit, from the file's own
state, whether or not anything is applied: the previous value of every track
property the edit touches, the chapter document the file had and the tag
document the file had, both exactly as the extractor printed them. On an
applied edit it is written to disk *before* the editor runs -- to the
directory the caller names, or ``<[paths].work>/rollback`` -- and an edit whose
rollback cannot be written does not happen. Generating it is cheap; having to
reconstruct it afterwards is not possible.

**Dry run by default.** Nothing here writes unless it is told twice: once by
being called, and once by ``dry_run=False``.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import logging
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from . import tags as tags_module
from .chapters import xml as chapters_xml
from .config import Config
from .langcodes import canonical
from .probe import MediaProbe, Track, container_mismatch, probe
from .run import Runner, default_runner

__all__ = [
    "DEFAULT_MUST_NOT_CHANGE",
    "PropeditError",
    "PropeditResult",
    "Rollback",
    "RollbackFiles",
    "TrackEdit",
    "default_rollback_dir",
    "safe_propedit",
]

log = logging.getLogger(__name__)

#: Fields of a track that no header edit in this package may change. They are
#: compared before and after, keyed on the track's identifier.
DEFAULT_MUST_NOT_CHANGE: Final[tuple[str, ...]] = (
    "type",
    "codec_id",
    "channels",
    "sample_rate",
    "pixel_dimensions",
    "display_dimensions",
    "number",
)

#: The editor exits 1 for warnings, which are routine.
_EDIT_OK: Final = (0, 1)


class PropeditError(RuntimeError):
    """An edit could not be attempted. A refused edit is a result, not this."""


@dataclass(frozen=True)
class TrackEdit:
    """What to change on one track, selected by the identifier it carries.

    ``None`` means "leave it alone". There is no value that means "clear the
    name": removing a track name is a different operation and it has not been
    needed, so it is not silently reachable by passing an empty string.
    """

    uid: int
    language: str | None = None
    name: str | None = None
    default: bool | None = None
    forced: bool | None = None
    enabled: bool | None = None

    @property
    def selector(self) -> str:
        """The editor's own way of naming one track by its identifier."""
        return f"track:={self.uid}"

    def properties(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if self.language is not None:
            out.append(("language", self.language))
        if self.name is not None:
            out.append(("name", self.name))
        for key, value in (
            ("flag-default", self.default),
            ("flag-forced", self.forced),
            ("flag-enabled", self.enabled),
        ):
            if value is not None:
                out.append((key, "1" if value else "0"))
        return out

    @property
    def is_empty(self) -> bool:
        return not self.properties()


@dataclass(frozen=True)
class RollbackFiles:
    """Where a rollback was written. The documents are absent when not captured."""

    tsv: Path
    chapters: Path | None = None
    tags: Path | None = None


@dataclass(frozen=True)
class Rollback:
    """Everything needed to put the file back, captured before the edit.

    ``rows`` is ``(track identifier, property, previous value)`` for every
    property the edit touches; an empty previous value means the file had
    none, and putting it back means deleting the property. The documents are
    the extractor's own output, so nothing the reader here does not model --
    nested marks, a name in a second language, a binary tag -- is lost from
    them.
    """

    path: Path
    track_edits: tuple[TrackEdit, ...] = ()
    chapters_xml: str | None = None
    tags_xml: str | None = None
    rows: tuple[tuple[int, str, str], ...] = ()

    def to_tsv(self, files: RollbackFiles | None = None) -> str:
        """One row per track property, for an audit log a person can read."""
        rows = ["path\ttrack_uid\tproperty\tprevious_value"]
        for uid, key, value in self.rows:
            rows.append(f"{self.path}\t{uid}\t{key}\t{value}")
        for name, document, where in (
            ("chapters", self.chapters_xml, files.chapters if files else None),
            ("tags", self.tags_xml, files.tags if files else None),
        ):
            if document is None:
                continue
            if not document.strip():
                rows.append(f"{self.path}\t-\t{name}\t(none)")
            elif where is not None:
                rows.append(f"{self.path}\t-\t{name}\t{where.name}")
            else:
                rows.append(f"{self.path}\t-\t{name}\t{len(document)} bytes captured")
        return "\n".join(rows) + "\n"

    def write(self, directory: Path, *, now: dt.datetime | None = None) -> RollbackFiles:
        """Put the rollback on disk. Never overwrites: an existing name is an error.

        The names carry the file's own name and the time to the microsecond,
        so two passes over the same file leave two artefacts, not one.
        """
        directory.mkdir(parents=True, exist_ok=True)
        moment = (now or dt.datetime.now(dt.UTC)).strftime("%Y%m%dT%H%M%S%fZ")
        stem = f"{self.path.name}.{moment}"
        chapters = tags = None
        if self.chapters_xml is not None and self.chapters_xml.strip():
            chapters = directory / f"{stem}.chapters.xml"
            _write_new(chapters, self.chapters_xml)
        if self.tags_xml is not None and self.tags_xml.strip():
            tags = directory / f"{stem}.tags.xml"
            _write_new(tags, self.tags_xml)
        files = RollbackFiles(
            tsv=directory / f"{stem}.rollback.tsv", chapters=chapters, tags=tags
        )
        _write_new(files.tsv, self.to_tsv(files))
        return files

    def restore_command(self, files: RollbackFiles | None = None) -> list[str]:
        """The header editor's arguments that put every captured value back."""
        argv: list[str] = [str(self.path)]
        for uid, key, value in self.rows:
            argv += ["--edit", f"track:={uid}"]
            argv += ["--set", f"{key}={value}"] if value else ["--delete", key]
        if self.chapters_xml is not None:
            # An empty document is how the editor is told to remove every mark.
            argv += ["--chapters", str(files.chapters) if files and files.chapters else ""]
        if self.tags_xml is not None:
            argv += ["--tags", f"all:{files.tags}" if files and files.tags else "all:"]
        return argv


def _write_new(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def default_rollback_dir(config: Config | None) -> Path:
    """Where a rollback goes when nobody said: ``<[paths].work>/rollback``."""
    return (config if config is not None else Config()).paths.work / "rollback"


@dataclass(frozen=True)
class PropeditResult:
    """What was asked, what was done, and what the file says about it afterwards."""

    path: Path
    command: tuple[str, ...] = ()
    applied: bool = False
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    rollback: Rollback | None = None
    before: MediaProbe | None = None
    after: MediaProbe | None = None
    changed: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def __str__(self) -> str:
        state = "applied" if self.applied else "dry run"
        head = f"{self.path.name}: {state}, {len(self.changed)} change(s)"
        lines = [head, *(f"  problem: {p}" for p in self.problems),
                 *(f"  note: {n}" for n in self.notes)]
        return "\n".join(lines)


def safe_propedit(
    path: Path | str,
    edits: Sequence[TrackEdit] = (),
    *,
    chapters: chapters_xml.ChapterSet | None = None,
    tags: tags_module.TagSet | None = None,
    title: str | None = None,
    dry_run: bool = True,
    must_not_change: Sequence[str] = DEFAULT_MUST_NOT_CHANGE,
    runner: Runner | None = None,
    config: Config | None = None,
    work_dir: Path | None = None,
    rollback_dir: Path | None = None,
) -> PropeditResult:
    """Apply header edits to one file and prove that nothing else moved.

    Returns a result rather than raising for a refused edit: a pass over a
    collection has to record what it did not do as carefully as what it did.
    An applied edit first writes its rollback to ``rollback_dir`` (by default
    :func:`default_rollback_dir`); if that fails, nothing is edited.
    """
    target = Path(path)
    run = runner if runner is not None else default_runner(config)
    before = probe(target, runner=run)

    problems: list[str] = []
    notes: list[str] = []

    mismatch = container_mismatch(before)
    if mismatch is not None:
        return PropeditResult(target, problems=(mismatch,), before=before)
    if not before.container.is_matroska:
        return PropeditResult(
            target,
            problems=(f"{target.name} is not a Matroska file; the header editor "
                      "cannot change it and would report success anyway",),
            before=before,
        )

    wanted = [edit for edit in edits if not edit.is_empty]
    problems.extend(_check_tracks(before, wanted))
    problems.extend(_check_language_overrides(before, wanted, tags))
    if not (wanted or chapters is not None or tags is not None or title is not None):
        problems.append("nothing to do: no edit was asked for")
    if chapters is not None and chapters.unkept:
        problems.append(
            "the chapter document has structure that would be lost on writing: "
            + "; ".join(chapters.unkept)
        )
    if tags is not None and tags.unkept:
        problems.append(
            "the tag document has element(s) that would be lost on writing: "
            + ", ".join(tags.unkept)
        )
    if problems:
        return PropeditResult(target, problems=tuple(problems), before=before)

    rollback = _rollback_for(target, before, wanted, chapters, tags, run)

    with tempfile.TemporaryDirectory(dir=work_dir) as scratch:
        scratch_dir = Path(scratch)
        argv = _command(target, wanted, chapters, tags, title, scratch_dir)
        if dry_run:
            return PropeditResult(
                target,
                command=tuple(argv),
                applied=False,
                notes=("dry run: nothing was written",),
                rollback=rollback,
                before=before,
                changed=tuple(_intended(wanted, chapters, tags, title)),
            )
        # The rollback reaches the disk before the file is touched. If it
        # cannot be written this raises, and the edit does not happen.
        files = rollback.write(
            rollback_dir if rollback_dir is not None else default_rollback_dir(config)
        )
        notes.append(f"rollback written to {files.tsv}")
        result = run("mkvpropedit", argv, ok=_EDIT_OK)
        if result.returncode == 1:
            lines = result.tail.splitlines()
            said = lines[-1][:200] if lines else "(it gave no message)"
            notes.append(f"the editor warned: {said}")

    after = probe(target, runner=run)
    problems.extend(
        _compare(before, after, wanted, chapters, tags, title, must_not_change)
    )
    problems.extend(_verify_tags(target, tags, run))
    return PropeditResult(
        target,
        command=tuple(argv),
        applied=True,
        problems=tuple(problems),
        notes=tuple(notes),
        rollback=rollback,
        before=before,
        after=after,
        changed=tuple(_intended(wanted, chapters, tags, title)),
    )


# ------------------------------------------------------------------- the checks
def _check_tracks(before: MediaProbe, edits: Sequence[TrackEdit]) -> list[str]:
    problems: list[str] = []
    seen: set[int] = set()
    for edit in edits:
        if edit.uid in seen:
            problems.append(f"track {edit.uid} is edited twice in one call")
        seen.add(edit.uid)
        if before.track_by_uid(edit.uid) is None:
            problems.append(f"no track in this file carries the identifier {edit.uid}")
    return problems


def _check_language_overrides(
    before: MediaProbe,
    edits: Sequence[TrackEdit],
    tags: tags_module.TagSet | None,
) -> list[str]:
    """Refuse a header language change that a tag would go on overruling."""
    problems: list[str] = []
    for edit in edits:
        if edit.language is None:
            continue
        track = before.track_by_uid(edit.uid)
        if track is None or track.tag_language is None:
            continue
        wanted = canonical(edit.language)
        if tags is not None and tags.language_of(edit.uid) == wanted:
            continue
        if track.tag_language == wanted:
            continue
        problems.append(
            f"track {edit.uid}: a tag element claims {track.tag_language} and would "
            f"go on overruling a header set to {edit.language}; rewrite the tag in "
            "the same call (mkvkit.tags.set_track_language) or do not make this edit"
        )
    return problems


def _rollback_for(
    path: Path,
    before: MediaProbe,
    edits: Sequence[TrackEdit],
    chapters: chapters_xml.ChapterSet | None,
    tags: tags_module.TagSet | None,
    run: Runner,
) -> Rollback:
    inverse: list[TrackEdit] = []
    rows: list[tuple[int, str, str]] = []
    for edit in edits:
        track = before.track_by_uid(edit.uid)
        if track is None:
            continue
        inverse.append(
            TrackEdit(
                uid=edit.uid,
                language=track.language_raw if edit.language is not None else None,
                name=track.name if edit.name is not None else None,
                default=track.default if edit.default is not None else None,
                forced=track.forced if edit.forced is not None else None,
                enabled=track.enabled if edit.enabled is not None else None,
            )
        )
        previous = {
            "language": track.language_raw or "",
            "name": track.name or "",
            "flag-default": "1" if track.default else "0",
            "flag-forced": "1" if track.forced else "0",
            "flag-enabled": "1" if track.enabled else "0",
        }
        rows.extend((edit.uid, key, previous[key]) for key, _ in edit.properties())
    # The documents are kept exactly as the extractor printed them, not as
    # this package's model of them: the model does not carry everything a
    # file can hold, and a rollback that drops what it does not understand
    # is a second deletion.
    chapters_document: str | None = None
    if chapters is not None:
        chapters_document = _extracted(run, path, "chapters")
    tags_document: str | None = None
    if tags is not None:
        tags_document = _extracted(run, path, "tags")
    return Rollback(path, tuple(inverse), chapters_document, tags_document, tuple(rows))


def _extracted(run: Runner, path: Path, what: str) -> str:
    text = run("mkvextract", [str(path), what], ok=(0, 1)).stdout
    return text.lstrip("\ufeff") if text.strip() else ""


def _command(
    path: Path,
    edits: Sequence[TrackEdit],
    chapters: chapters_xml.ChapterSet | None,
    tags: tags_module.TagSet | None,
    title: str | None,
    scratch: Path,
) -> list[str]:
    """One invocation for every change.

    Every call is one modification-time bump, and that bump is expensive
    downstream -- a media server rescans on it. Two edits in one call are
    therefore not a micro-optimisation, they are the difference between one
    rescan and two.
    """
    argv: list[str] = [str(path)]
    if title is not None:
        argv += ["--edit", "info", "--set", f"title={title}"]
    for edit in edits:
        argv += ["--edit", edit.selector]
        for key, value in edit.properties():
            argv += ["--set", f"{key}={value}"]
    if chapters is not None:
        document = scratch / "chapters.xml"
        document.write_text(chapters_xml.build(chapters), encoding="utf-8", newline="\n")
        argv += ["--chapters", str(document)]
    if tags is not None:
        document = scratch / "tags.xml"
        document.write_text(tags_module.build(tags), encoding="utf-8", newline="\n")
        argv += ["--tags", f"all:{document}"]
    return argv


def _intended(
    edits: Sequence[TrackEdit],
    chapters: chapters_xml.ChapterSet | None,
    tags: tags_module.TagSet | None,
    title: str | None,
) -> list[str]:
    changed = [
        f"track {edit.uid}: {key}={value}"
        for edit in edits
        for key, value in edit.properties()
    ]
    if chapters is not None:
        changed.append(f"chapters: {len(chapters)} mark(s)")
    if tags is not None:
        changed.append(f"tags: {len(tags)} tag(s)")
    if title is not None:
        changed.append(f"title: {title!r}")
    return changed


def _compare(
    before: MediaProbe,
    after: MediaProbe,
    edits: Sequence[TrackEdit],
    chapters: chapters_xml.ChapterSet | None,
    tags: tags_module.TagSet | None,
    title: str | None,
    must_not_change: Sequence[str],
) -> list[str]:
    """Everything that had to stay the same, and everything that had to change."""
    problems: list[str] = []
    if len(after.tracks) != len(before.tracks):
        problems.append(
            f"the track count changed: {len(before.tracks)} -> {len(after.tracks)}"
        )
        return problems

    wanted = {edit.uid: edit for edit in edits}
    for old in before.tracks:
        if old.uid is None:
            # Nothing to match it against on the other side; the count check
            # above is all that can be said about such a track.
            continue
        new = after.track_by_uid(old.uid)
        if new is None:
            problems.append(f"track {old.uid} is not in the file any more")
            continue
        for name in must_not_change:
            if getattr(old, name) != getattr(new, name):
                problems.append(
                    f"track {old.uid}: {name} changed "
                    f"{getattr(old, name)!r} -> {getattr(new, name)!r}"
                )
        problems.extend(_compare_one(old, new, wanted.get(old.uid)))

    if chapters is None:
        if before.chapter_count != after.chapter_count:
            problems.append(
                f"the chapters changed without being asked to: "
                f"{before.chapter_count} -> {after.chapter_count} mark(s)"
            )
    elif after.chapter_count != len(chapters):
        problems.append(
            f"{len(chapters)} mark(s) were written and the file reports "
            f"{after.chapter_count}"
        )
    if tags is None and before.tag_count != after.tag_count:
        problems.append(
            f"the tags changed without being asked to: "
            f"{before.tag_count} -> {after.tag_count}"
        )
    expected_title = title if title is not None else before.container.title
    if (after.container.title or "") != (expected_title or ""):
        problems.append(
            f"the container title is {after.container.title!r}, expected "
            f"{expected_title!r}"
        )
    return problems


def _compare_one(old: Track, new: Track, edit: TrackEdit | None) -> list[str]:
    """One track, field by field: what was asked for, and nothing else."""
    problems: list[str] = []
    asked_for: tuple[tuple[str, object], ...] = (
        ("language", canonical(edit.language) if edit and edit.language else None),
        ("name", edit.name if edit else None),
        ("default", edit.default if edit else None),
        ("forced", edit.forced if edit else None),
        ("enabled", edit.enabled if edit else None),
    )
    for label, asked in asked_for:
        old_value = getattr(old, label)
        new_value = getattr(new, label)
        if asked is None:
            if old_value != new_value:
                problems.append(
                    f"track {old.uid}: {label} changed without being asked to "
                    f"({old_value!r} -> {new_value!r})"
                )
        elif new_value != asked:
            problems.append(
                f"track {old.uid}: {label} was set to {asked!r} and reads "
                f"{new_value!r}"
            )
    return problems


def _verify_tags(
    path: Path, tags: tags_module.TagSet | None, run: Runner
) -> list[str]:
    """The tag document that went in has to be the one that comes back out.

    Compared as triples: the editor's own normal form differs from anything a
    caller writes, and a comparison that does not know this reports every run
    as a total loss.
    """
    if tags is None:
        return []
    written = tags_module.read_tags(path, runner=run)
    if written.triples == tags.triples:
        return []
    missing = sorted(set(tags.triples) - set(written.triples))
    extra = sorted(set(written.triples) - set(tags.triples))
    problems = []
    if missing:
        problems.append(f"{len(missing)} tag value(s) did not survive the write: {missing[:3]}")
    if extra:
        problems.append(f"{len(extra)} tag value(s) appeared that were not written: {extra[:3]}")
    return problems


def with_language_tag_fix(
    found: MediaProbe, edit: TrackEdit, existing: tags_module.TagSet
) -> tuple[TrackEdit, tags_module.TagSet | None]:
    """The pair of edits a language change actually needs.

    Returns the edit unchanged and the tag set rewritten where the file has a
    tag that would overrule the header, or ``None`` where it has none and
    there is nothing to write.
    """
    if edit.language is None:
        return edit, None
    track = found.track_by_uid(edit.uid)
    if track is None or track.tag_language is None:
        return edit, None
    return edit, tags_module.set_track_language(existing, edit.uid, edit.language)
