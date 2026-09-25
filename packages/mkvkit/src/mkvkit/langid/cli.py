"""mkvkit.langid.cli -- the sub-commands, and what each of them refuses to do.

Three verbs, in the order a pass uses them:

``jobs``    walk a directory and write the list of tracks to look at;
``scan``    read those tracks and record what a detector heard;
``report``  decide, queue what is left, and write both.

They are separate commands rather than one because the expensive one is in the
middle and the other two must be runnable without it. ``jobs`` needs a probe
program; ``report`` needs nothing at all and can be re-run against evidence
collected months ago, which is what makes a threshold adjustable.

Nothing here writes to a media file. This whole sub-command reads.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from ..config import Config
from .ladder import SettleBar
from .report import render_markdown, render_tsv, summarise
from .review import decide_all, rescan_queue
from .sources import FilesystemSource, read_jobs, write_jobs
from .worker import FfmpegExtractor, ResultLog, scan_track

__all__ = ["register"]

log = logging.getLogger(__name__)


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    langid = subparsers.add_parser(
        "langid", help="identify the spoken language of audio tracks"
    )
    verbs = langid.add_subparsers(dest="verb", metavar="VERB")

    jobs = verbs.add_parser("jobs", help="list the tracks to look at")
    jobs.add_argument("roots", nargs="+", type=Path, metavar="PATH")
    jobs.add_argument("--out", type=Path, default=Path("jobs.jsonl"))
    jobs.add_argument(
        "--only-unknown", action="store_true",
        help="skip tracks that already claim a language",
    )
    jobs.set_defaults(handler=_jobs)

    scan = verbs.add_parser("scan", help="read the tracks and record what was heard")
    scan.add_argument("--jobs", type=Path, default=Path("jobs.jsonl"))
    scan.add_argument("--out", type=Path, default=Path("results.jsonl"))
    scan.add_argument("--windows", type=int, default=5)
    scan.add_argument("--window-seconds", type=float, default=20.0)
    scan.add_argument("--limit", type=int, default=0)
    scan.set_defaults(handler=_scan)

    report = verbs.add_parser("report", help="decide, and say what is left")
    report.add_argument("--results", type=Path, default=Path("results.jsonl"))
    report.add_argument("--out-dir", type=Path, default=None)
    report.set_defaults(handler=_report)

    langid.set_defaults(handler=_no_verb, _parser=langid)


def _no_verb(args: argparse.Namespace, _config: Config) -> int:
    args._parser.print_help()
    return 2


def _jobs(args: argparse.Namespace, config: Config) -> int:
    from ..tools import find_tool

    source = FilesystemSource(
        roots=args.roots,
        ffprobe=find_tool("ffprobe", config=config),
        only_unknown=args.only_unknown,
    )
    written = write_jobs(args.out, source.jobs())
    print(f"{written} track(s) -> {args.out}")
    return 0


def _scan(args: argparse.Namespace, config: Config) -> int:
    """Read every track in the list that is not already in the result log.

    The model is constructed once, after the job list has been read, so a
    mistyped path fails in a second rather than after a model load.
    """
    from ..tools import find_tool
    from .worker import WhisperDetector

    jobs = list(read_jobs(args.jobs))
    if args.limit:
        jobs = jobs[: args.limit]
    log_file = ResultLog(args.out)
    todo = [
        job for job in jobs
        if not log_file.done((job.path, job.stream_index, config.langid.model))
    ]
    if not todo:
        print("nothing to do: every track in the list is already in the log")
        return 0
    print(f"{len(todo)} of {len(jobs)} track(s) to read")

    detector = WhisperDetector(
        config.langid.model, device=config.langid.device,
        download_root=config.langid.model_dir,
    )
    extractor = FfmpegExtractor(ffmpeg=find_tool("ffmpeg", config=config))
    for job in todo:
        evidence = scan_track(
            job.path, job.stream_index, job.audio_ord, detector,
            extractor=extractor, speech=detector, count=args.windows,
            window_s=args.window_seconds, runtime_s=job.runtime_s,
            existing_tag=job.tag,
        )
        log_file.append(evidence)
    print(f"{len(todo)} track(s) -> {args.out}")
    return 0


def _report(args: argparse.Namespace, config: Config) -> int:
    bar = SettleBar(
        min_conf=config.langid.min_conf, override_conf=config.langid.override_conf
    )
    evidence = list(ResultLog(args.results).latest().values())
    outcomes = list(decide_all(evidence, bar=bar))
    queue = rescan_queue(outcomes, bar=bar)
    markdown = render_markdown(outcomes, queue, bar=bar, scope=str(args.results))
    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        (args.out_dir / "langid-report.md").write_text(
            markdown, encoding="utf-8", newline="\n"
        )
        (args.out_dir / "langid-results.tsv").write_text(
            render_tsv(outcomes), encoding="utf-8", newline="\n"
        )
        print(f"{len(outcomes)} track(s) -> {args.out_dir}")
    else:
        print(markdown)
    summary = summarise(outcomes, bar)
    log.info(
        "%d track(s), %d would be written, %d still open",
        summary.total, summary.writes, len(queue),
    )
    return 0
