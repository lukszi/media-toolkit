"""mkvkit.commands -- the file-side sub-commands.

Seven verbs, in the order a job uses them: look at the file, read its marks
and its tags, plan and run a rebuild, edit a header in place, prove the result,
and finally put it where the old file was.

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
import logging
from pathlib import Path

from . import remux as remux_module
from . import swap as swap_module
from . import tags as tags_module
from . import verify as verify_module
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
