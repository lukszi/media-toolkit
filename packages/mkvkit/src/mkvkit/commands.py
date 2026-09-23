"""mkvkit.commands -- the file-side sub-commands.

Seven verbs, in the order a job uses them: look at the file, read its marks
and its tags, plan and run a rebuild, edit a header in place, prove the result,
and finally put it where the old file was.

``chapters`` has a second group of verbs under it for the question a published
name list raises: which of the three jobs this candidate is (``classify``),
whether its names may go onto marks you already have (``match``), the text
each mark would be judged against (``windows``), how names that were written
rather than sourced grade against that text (``selfcheck``), and the whole
edit as one object before anything is written (``plan``).

Two properties are the same in every one of them.

**Reading is free; writing is asked for twice.** Every verb that changes a
file takes ``--dry-run`` (which is the default) and ``--apply``. There is no
third state and no environment variable that flips it.

**The exit code means something.** Nothing here relies on somebody reading the
output: a refusal, a failed comparison or a container that is not what its
name says all exit non-zero, so these commands can be used in a pipeline
where "it printed something" is not a signal.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from . import plan as plan_module
from . import remux as remux_module
from . import swap as swap_module
from . import tags as tags_module
from . import verify as verify_module
from .chapters import names as chapter_names
from .chapters import selfcheck as chapter_selfcheck
from .chapters import sources as chapter_sources
from .chapters import transcripts as chapter_transcripts
from .chapters import verify as chapter_verify
from .chapters import windows as chapter_windows
from .chapters import xml as chapters_xml
from .config import Config
from .probe import container_mismatch, describe, probe
from .propedit import TrackEdit, safe_propedit

__all__ = ["REGISTRARS", "add_write_arguments"]

log = logging.getLogger(__name__)


def add_write_arguments(parser: argparse.ArgumentParser) -> None:
    """``--dry-run`` and ``--apply``, the only two states there are."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run", dest="apply", action="store_false", default=False,
        help="say what would happen and change nothing (the default)",
    )
    group.add_argument(
        "--apply", dest="apply", action="store_true",
        help="actually write",
    )


# ----------------------------------------------------------------------- probe
def _register_probe(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "probe", help="what is in a file, from both programs that can say"
    )
    parser.add_argument("paths", nargs="+", type=Path, metavar="PATH")
    parser.add_argument(
        "--no-elements", action="store_true",
        help="skip the element scan that reports whether there is a seek index",
    )
    parser.set_defaults(handler=_probe)


def _probe(args: argparse.Namespace, config: Config) -> int:
    problems = 0
    for path in args.paths:
        found = probe(path, config=config, elements=not args.no_elements)
        for line in describe(found):
            print(line)
        mismatch = container_mismatch(found)
        if mismatch is not None:
            print(f"  PROBLEM: {mismatch}")
            problems += 1
    return 1 if problems else 0


# -------------------------------------------------------------------- chapters
def _register_chapters(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("chapters", help="read, check and write chapter marks")
    verbs = parser.add_subparsers(dest="verb", metavar="VERB")

    show = verbs.add_parser("show", help="the marks a file carries")
    show.add_argument("path", type=Path)
    show.set_defaults(handler=_chapters_show)

    check = verbs.add_parser("check", help="run the self-check over a document")
    check.add_argument("document", type=Path)
    check.add_argument("--against", type=Path, default=None,
                       help="the file it would be applied to")
    check.set_defaults(handler=_chapters_check)

    rollback = verbs.add_parser(
        "rollback", help="write the document that undoes a naming pass"
    )
    rollback.add_argument("path", type=Path)
    rollback.add_argument("--out", type=Path, required=True)
    rollback.set_defaults(handler=_chapters_rollback)

    classify = verbs.add_parser(
        "classify", help="which of the three jobs a candidate list is, for this file"
    )
    classify.add_argument("path", type=Path)
    classify.add_argument("--candidate", type=Path, required=True,
                          help="a chapter document offered for this file")
    classify.add_argument("--runtime", type=float, default=None, metavar="SECONDS")
    classify.set_defaults(handler=_chapters_classify)

    match = verbs.add_parser(
        "match", help="put a candidate's names onto this file's own marks, or refuse"
    )
    match.add_argument("path", type=Path)
    match.add_argument("--candidate", type=Path, required=True)
    match.add_argument("--evidence", type=Path, default=None,
                       help="collected evidence, one object per line")
    match.add_argument("--language", default="eng",
                       help="the language the names are supposed to be in")
    match.add_argument("--out", type=Path, default=None,
                       help="write the resulting document here")
    match.set_defaults(handler=_chapters_match)

    windows = verbs.add_parser(
        "windows", help="the text each mark would be judged against"
    )
    windows.add_argument("path", type=Path)
    windows.add_argument("--cues", type=Path, required=True,
                         help="a subtitle or transcript file for this film")
    windows.add_argument("--language", default="eng")
    windows.add_argument("--out", type=Path, default=None)
    windows.set_defaults(handler=_chapters_windows)

    selfcheck = verbs.add_parser(
        "selfcheck", help="grade a written name list against its own windows"
    )
    selfcheck.add_argument("path", type=Path)
    selfcheck.add_argument("--cues", type=Path, required=True)
    selfcheck.add_argument("--names", type=Path, required=True)
    selfcheck.add_argument("--language", default="eng")
    selfcheck.set_defaults(handler=_chapters_selfcheck)

    plan = verbs.add_parser(
        "plan", help="everything that would change about this file, in one object"
    )
    plan.add_argument("path", type=Path)
    plan.add_argument("--candidate", type=Path, required=True)
    plan.add_argument("--evidence", type=Path, default=None)
    plan.add_argument("--language", default="eng")
    plan.add_argument("--source", required=True,
                      help="where the names came from; it is written into the file")
    add_write_arguments(plan)
    plan.set_defaults(handler=_chapters_plan)

    apply_marks = verbs.add_parser("apply", help="write a document into a file")
    apply_marks.add_argument("path", type=Path)
    apply_marks.add_argument("--document", type=Path, required=True)
    add_write_arguments(apply_marks)
    apply_marks.set_defaults(handler=_chapters_apply)

    parser.set_defaults(handler=_no_verb, _parser=parser)


def _chapters_show(args: argparse.Namespace, config: Config) -> int:
    chapters = chapters_xml.read_chapters(args.path, config=config)
    print(f"{args.path.name}: {len(chapters)} mark(s), {chapters.named_count} named")
    for position, chapter in enumerate(chapters, 1):
        print(f"  {position:>3} {chapter.start_s:10.3f}  {chapter.name or ''}")
    return 0


def _chapters_check(args: argparse.Namespace, config: Config) -> int:
    chapters = chapters_xml.parse(args.document.read_text(encoding="utf-8"))
    runtime = None
    file_count = None
    if args.against is not None:
        found = probe(args.against, config=config, elements=False)
        runtime = found.container.duration_s
        file_count = found.chapter_count or None
    problems = chapters_xml.selfcheck(
        chapters, runtime_s=runtime, file_count=file_count
    )
    print(f"{args.document.name}: {len(chapters)} mark(s)")
    for problem in problems:
        print(f"  {problem}")
    return 1 if any(problem.blocking for problem in problems) else 0


def _chapters_rollback(args: argparse.Namespace, config: Config) -> int:
    chapters = chapters_xml.read_chapters(args.path, config=config)
    if not chapters:
        print(f"{args.path.name} has no marks; there is nothing to roll back to")
        return 1
    args.out.write_text(chapters_xml.rollback(chapters), encoding="utf-8", newline="\n")
    print(f"{len(chapters)} mark(s), names stripped -> {args.out}")
    return 0


def _chapters_apply(args: argparse.Namespace, config: Config) -> int:
    chapters = chapters_xml.parse(args.document.read_text(encoding="utf-8"))
    result = safe_propedit(
        args.path, chapters=chapters, dry_run=not args.apply, config=config
    )
    print(result)
    return 0 if result.ok else 1


def _read_document(path: Path) -> chapters_xml.ChapterSet:
    return chapters_xml.parse(path.read_text(encoding="utf-8"))


def _read_evidence(path: Path) -> list[chapter_verify.MarkEvidence]:
    """Collected evidence, one JSON object per line.

    Collecting it needs a decoder and a speech model; reading it back needs
    neither, so the judging half of this runs anywhere -- including on a
    machine that has never been near the media.
    """
    out: list[chapter_verify.MarkEvidence] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        out.append(
            chapter_verify.MarkEvidence(
                index=int(record.get("index", len(out) + 1)),
                time_s=float(record.get("time_s", 0.0)),
                name=record.get("name"),
                transcript=str(record.get("transcript") or ""),
                note=str(record.get("note") or ""),
            )
        )
    return out


def _read_names(path: Path) -> list[str | None]:
    record = json.loads(path.read_text(encoding="utf-8"))
    values = record["names"] if isinstance(record, dict) else record
    return [None if value is None else str(value) for value in values]


def _build_windows(
    args: argparse.Namespace, config: Config
) -> tuple[chapter_windows.WindowSet | None, int]:
    marks = chapters_xml.read_chapters(args.path, config=config)
    if not marks:
        print(f"{args.path.name} has no marks; there is nothing to cut into windows")
        return None, 1
    found = probe(args.path, config=config, elements=False)
    runtime = found.container.duration_s or 0.0
    cues = chapter_transcripts.parse_srt(args.cues.read_text(encoding="utf-8"))
    cues = chapter_transcripts.trim_to_marks(cues, marks.starts_s)
    refused = chapter_windows.refusals(cues, runtime_s=runtime)
    for reason in refused:
        print(f"  refused: {reason}")
    if refused:
        return None, 1
    built = chapter_windows.build_windows(
        cues, marks, runtime_s=runtime, names_language=args.language,
        source=args.cues.name,
    )
    return built, 0


def _chapters_classify(args: argparse.Namespace, config: Config) -> int:
    marks = chapters_xml.read_chapters(args.path, config=config)
    candidate = _read_document(args.candidate)
    runtime = args.runtime
    if runtime is None:
        runtime = probe(args.path, config=config, elements=False).container.duration_s
    found = chapter_sources.classify(
        marks if len(marks) else None, candidate, runtime_s=runtime
    )
    print(f"{args.path.name}: {found}")
    if found.needs_a_person:
        print("  this writes marks as well as names; read the list before applying")
    return 0 if found.usable else 1


def _chapters_match(args: argparse.Namespace, config: Config) -> int:
    marks = chapters_xml.read_chapters(args.path, config=config)
    candidate = _read_document(args.candidate)
    evidence = _read_evidence(args.evidence) if args.evidence else None
    result = chapter_names.match_names(
        marks, candidate, evidence=evidence, language=args.language
    )
    print(f"{args.path.name}: {result}")
    if result.shifts is not None:
        print(f"  {result.shifts}")
    if result.accepted and result.chapters is not None and args.out is not None:
        args.out.write_text(
            chapters_xml.build(result.chapters, language=args.language),
            encoding="utf-8", newline="\n",
        )
        print(f"  wrote {args.out}")
    return 0 if result.accepted else 1


def _chapters_windows(args: argparse.Namespace, config: Config) -> int:
    built, code = _build_windows(args, config)
    if built is None:
        return code
    text = built.render()
    if args.out is not None:
        written = chapter_transcripts.write_windows(args.out, text)
        print(
            f"{len(built)} window(s) -> {args.out}"
            + ("" if written else " (already current)")
        )
        return 0
    print(text)
    return 0


def _chapters_selfcheck(args: argparse.Namespace, config: Config) -> int:
    built, code = _build_windows(args, config)
    if built is None:
        return code
    names = _read_names(args.names)
    if len(names) != len(built):
        print(f"{len(names)} name(s) for {len(built)} window(s)")
        return 1
    graded = chapter_selfcheck.grade_names(names, built, language=args.language)
    print(f"{args.path.name}: {graded}")
    return 0 if graded.clean else 1


def _chapters_plan(args: argparse.Namespace, config: Config) -> int:
    marks = chapters_xml.read_chapters(args.path, config=config)
    candidate = _read_document(args.candidate)
    evidence = _read_evidence(args.evidence) if args.evidence else None
    result = chapter_names.match_names(
        marks, candidate, evidence=evidence, language=args.language
    )
    # The file's own tags are only read where the names are going in: a
    # refusal should not cost a read of a file nothing is going to be written
    # to, and on a collection most candidates are refusals.
    existing = tags_module.read_tags(args.path, config=config) if result.accepted else None
    plan = plan_module.chapter_names_plan(
        args.path, result, source=args.source, existing_tags=existing
    )
    print(plan)
    if plan.blocked:
        return 1
    outcome = plan_module.apply(plan, dry_run=not args.apply, config=config)
    print(outcome)
    return 0 if outcome.ok else 1


# ------------------------------------------------------------------------ tags
def _register_tags(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("tags", help="the tag element, and what it overrules")
    verbs = parser.add_subparsers(dest="verb", metavar="VERB")

    show = verbs.add_parser("show", help="the tags a file carries")
    show.add_argument("path", type=Path)
    show.set_defaults(handler=_tags_show)

    parser.set_defaults(handler=_no_verb, _parser=parser)


def _tags_show(args: argparse.Namespace, config: Config) -> int:
    found = probe(args.path, config=config, elements=False)
    tags = tags_module.read_tags(args.path, config=config)
    print(f"{args.path.name}: {len(tags)} tag(s)")
    for key, name, value in tags.triples:
        print(f"  {key or 'the whole file':<24} {name:<28} {value}")
    disagreements = found.language_disagreements
    for disagreement in disagreements:
        print(f"  PROBLEM: {disagreement}")
    return 1 if disagreements else 0


# -------------------------------------------------------------------- propedit
def _register_propedit(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "propedit", help="edit a track header in place, and prove nothing else moved"
    )
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--track", type=int, required=True, metavar="UID",
        help="the identifier the track carries (mkvkit probe prints them)",
    )
    parser.add_argument("--language", default=None)
    parser.add_argument("--name", default=None)
    parser.add_argument("--default", dest="default", action="store_true", default=None)
    parser.add_argument("--no-default", dest="default", action="store_false")
    parser.add_argument("--forced", dest="forced", action="store_true", default=None)
    parser.add_argument("--no-forced", dest="forced", action="store_false")
    parser.add_argument(
        "--fix-tag", action="store_true",
        help="rewrite a language tag that would overrule the header",
    )
    add_write_arguments(parser)
    parser.set_defaults(handler=_propedit)


def _propedit(args: argparse.Namespace, config: Config) -> int:
    edit = TrackEdit(
        uid=args.track, language=args.language, name=args.name,
        default=args.default, forced=args.forced,
    )
    tags = None
    if args.fix_tag and args.language:
        tags = tags_module.set_track_language(
            tags_module.read_tags(args.path, config=config), args.track, args.language
        )
    result = safe_propedit(
        args.path, [edit], tags=tags, dry_run=not args.apply, config=config
    )
    print(result)
    if not args.apply:
        for line in result.command:
            log.debug("would run: %s", line)
    return 0 if result.ok else 1


# ------------------------------------------------------------------------ verify
def _register_verify(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "verify", help="prove a rebuilt file is the original plus the intended change"
    )
    parser.add_argument("original", type=Path)
    parser.add_argument("built", type=Path)
    parser.add_argument(
        "--dropped", default="",
        help="stream indexes that were removed on purpose, comma separated",
    )
    parser.add_argument(
        "--appended", type=int, default=0,
        help="how many streams were added at the end",
    )
    parser.add_argument(
        "--header-only", action="store_true",
        help="a header edit: the payloads provably did not move, so skip hashing",
    )
    parser.add_argument("--default-moved", action="store_true")
    parser.set_defaults(handler=_verify)


def _verify(args: argparse.Namespace, config: Config) -> int:
    delta: verify_module.ExpectedDelta
    if args.header_only:
        delta = verify_module.HeaderOnly()
    elif args.appended:
        delta = verify_module.TracksAppended(count=args.appended)
    else:
        dropped = frozenset(
            int(value) for value in args.dropped.split(",") if value.strip()
        )
        delta = verify_module.TracksDropped(
            dropped=dropped, default_moved=args.default_moved
        )
    take_hashes = not args.header_only
    original = verify_module.collect(args.original, hashes=take_hashes, config=config)
    built = verify_module.collect(args.built, hashes=take_hashes, config=config)
    result = verify_module.compare(original, built, delta)
    print(result)
    return 0 if result.ok else 1


# ------------------------------------------------------------------------ remux
def _register_remux(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "remux", help="rebuild a file without the tracks a policy allows dropping"
    )
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--staging", type=Path, default=None,
        help="where the rebuild is written; otherwise [paths].staging",
    )
    parser.add_argument(
        "--original-language", default=None,
        help="the language the work was made in; nothing is dropped without it",
    )
    parser.add_argument("--chapters", type=Path, default=None)
    add_write_arguments(parser)
    parser.set_defaults(handler=_remux)


def _remux(args: argparse.Namespace, config: Config) -> int:
    staging = args.staging if args.staging is not None else config.paths.staging
    if staging is None:
        print(
            "no staging directory: pass --staging or set [paths].staging. A rebuild "
            "is never written over the file it is reading."
        )
        return 2
    found = probe(args.path, config=config)
    chapters = (
        chapters_xml.parse(args.chapters.read_text(encoding="utf-8"))
        if args.chapters is not None
        else None
    )
    remux = remux_module.plan(
        found,
        remux_module.RemuxPolicy.from_config(config),
        output=Path(staging) / args.path.name,
        original_language=args.original_language,
        chapters=chapters,
    )
    print(remux)
    if not remux.ok:
        return 1
    if not remux.worthwhile:
        print("  nothing would change; not rebuilding")
        return 0
    result = remux_module.build(remux, dry_run=not args.apply, config=config)
    for note in result.notes:
        print(f"  note: {note}")
    for problem in result.problems:
        print(f"  problem: {problem}")
    if result.applied:
        print(f"  built {result.output}")
        print(
            "  now verify it:  mkvkit verify "
            f"{args.path} {result.output} --dropped "
            f"{','.join(str(i) for i in remux.drop_audio)}"
        )
    return 0 if result.ok else 1


# ------------------------------------------------------------------------- swap
def _register_swap(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "swap", help="park the original and put the rebuild in its place"
    )
    parser.add_argument("keeper", type=Path, help="the file that is live now")
    parser.add_argument("replacement", type=Path, help="the rebuilt file")
    parser.add_argument(
        "--parked", type=Path, default=None,
        help="where the original goes; otherwise [paths].parked",
    )
    add_write_arguments(parser)
    parser.set_defaults(handler=_swap)


def _swap(args: argparse.Namespace, config: Config) -> int:
    parked = args.parked if args.parked is not None else config.paths.parked
    if parked is None:
        print(
            "no parking directory: pass --parked or set [paths].parked. The file "
            "being replaced is moved there and kept."
        )
        return 2
    result = swap_module.swap(
        swap_module.SwapPair(args.keeper, args.replacement),
        parked_dir=parked,
        dry_run=not args.apply,
        config=config,
    )
    print(result)
    return 0 if result.ok else 1


def _no_verb(args: argparse.Namespace, _config: Config) -> int:
    args._parser.print_help()
    return 2


#: Sub-command name -> the function that adds it. The entry point walks this,
#: so a command that cannot be imported costs the others nothing.
REGISTRARS = {
    "probe": _register_probe,
    "chapters": _register_chapters,
    "tags": _register_tags,
    "propedit": _register_propedit,
    "verify": _register_verify,
    "remux": _register_remux,
    "swap": _register_swap,
}
