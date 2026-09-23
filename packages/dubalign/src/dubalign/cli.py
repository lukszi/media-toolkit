"""dubalign.cli -- sub-commands over the measurement and splice pipeline.

The verbs are the stages, in the order a job uses them: look at the file,
decode it once, map the offset across the whole runtime, measure it finely,
find the jumps, plan the joins, build the track, encode it, prove the result.

Everything the one-off scripts this grew out of had baked in -- the sources,
the program locations, the output names, the hand-found segment boundaries --
is an argument here, or measured output. The boundaries in particular: a
constant in a source file that was found by hand once is not a measurement,
and the plan is written as a document that can be read, diffed and re-run.

Two properties hold throughout, the same two the file-side entry point has.
**Reading is free and writing is asked for twice**: every verb that writes
takes the dry run, which is the default, or the explicit apply, and there is no
third state. **The exit code carries the answer**: a verification outside the
bar, a control that did not reproduce its known answer, and a plan that fails
its own self-check all exit non-zero, because these belong in a pipeline where
"it printed something" is not a signal.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from mkvkit.cli import SubCommand, add_common_arguments, run
from mkvkit.config import Config

from . import __version__
from .changepoints import find_changepoints, refine_changepoints
from .check_raw import check_signal
from .controls import array_controls, file_controls
from .decode import Decoded, decode
from .densemap import dense_map
from .drift import drift as measure_drift
from .drift import fit_rate
from .encode import ENCODINGS, encode
from .plan import SplicePlan, plan_from_measurements
from .probe import probe, reference_risk
from .splice import splice
from .verify_pcm import BAR_MS, verify_against

__all__ = ["REGISTRY", "build_parser", "main"]

log = logging.getLogger(__name__)


def add_write_arguments(parser: argparse.ArgumentParser) -> None:
    """``--dry-run`` and ``--apply``, the only two states there are."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run", dest="apply", action="store_false", default=False,
        help="say what would happen and write nothing (the default)",
    )
    group.add_argument("--apply", dest="apply", action="store_true", help="actually write")


def add_pair_arguments(parser: argparse.ArgumentParser) -> None:
    """The two sources every measurement verb takes, and where to work."""
    parser.add_argument("reference", type=Path, help="the timeline to align to")
    parser.add_argument("other", type=Path, help="the transfer to align")
    parser.add_argument("--reference-stream", type=int, default=0, metavar="N")
    parser.add_argument("--other-stream", type=int, default=0, metavar="N")
    parser.add_argument(
        "--work", type=Path, default=Path("./work"),
        help="where the decoded samples are kept and reused",
    )


def _decode_pair(args: argparse.Namespace, config: Config) -> tuple[Decoded, Decoded]:
    reference = decode(
        args.reference, out_dir=args.work, audio_index=args.reference_stream,
        name="reference", config=config,
    )
    other = decode(
        args.other, out_dir=args.work, audio_index=args.other_stream,
        name="other", config=config,
    )
    return reference, other


# ----------------------------------------------------------------------- probe
def _register_probe(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("probe", help="what audio a file carries")
    parser.add_argument("paths", nargs="+", type=Path, metavar="PATH")
    parser.set_defaults(handler=_probe)


def _probe(args: argparse.Namespace, config: Config) -> int:
    for path in args.paths:
        found = probe(path, config=config)
        for line in found.describe():
            print(line)
        for stream in found.streams:
            risk = reference_risk(stream)
            if risk is not None:
                # A note, not a refusal: the track is fine as material and
                # unfit as the thing everything else is measured against, and
                # only the person choosing knows which it is about to be.
                print(f"  NOTE a:{stream.audio_index} {risk}")
    return 0


# ---------------------------------------------------------------------- decode
def _register_decode(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "decode", help="one sequential read into the two dumps everything else uses"
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--stream", type=int, default=0, metavar="N")
    parser.add_argument("--work", type=Path, default=Path("./work"))
    parser.add_argument("--name", default=None, help="what to call the dumps")
    parser.add_argument("--rebuild", action="store_true", help="ignore dumps already there")
    parser.set_defaults(handler=_decode)


def _decode(args: argparse.Namespace, config: Config) -> int:
    decoded = decode(
        args.source, out_dir=args.work, audio_index=args.stream, name=args.name,
        reuse=not args.rebuild, config=config,
    )
    print(f"{decoded.full_path}  {decoded.frames} frames = {decoded.duration_s:.3f} s")
    print(f"{decoded.analysis_path}")
    check = check_signal(
        decoded.channel(0), decoded.sample_rate,
        declared_duration_s=decoded.stream.duration_s,
    )
    for line in check.describe():
        print(f"  {line}")
    return 0 if check.ok else 1


# ------------------------------------------------------------------------- map
def _register_map(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "map", help="the offset across the whole timeline, not at one point"
    )
    add_pair_arguments(parser)
    parser.add_argument("--window", type=float, default=10.0, metavar="SECONDS")
    parser.add_argument("--step", type=float, default=2.0, metavar="SECONDS")
    parser.add_argument("--all", action="store_true", help="print the unconfident points too")
    parser.set_defaults(handler=_map)


def _map(args: argparse.Namespace, config: Config) -> int:
    reference, other = _decode_pair(args, config)
    series = dense_map(
        reference.analysis(), other.analysis(), sr=reference.analysis_rate,
        window_s=args.window, step_s=args.step,
    )
    shown = series if args.all else series.confident()
    for point in shown:
        print(
            f"{point.t:9.2f}  {point.lag_ms:+10.2f} ms  r={point.r:.3f}  "
            f"peak/second={point.ratio:5.2f}"
        )
    print(shown.summary())
    return 0 if len(shown) else 1


# ----------------------------------------------------------------------- drift
def _register_drift(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "drift", help="the fine offset inside one stretch, and its rate"
    )
    add_pair_arguments(parser)
    parser.add_argument("--base-ms", type=float, required=True,
                        help="where the map said the offset is")
    parser.add_argument("--from", dest="start", type=float, default=None, metavar="SECONDS")
    parser.add_argument("--to", dest="end", type=float, default=None, metavar="SECONDS")
    parser.add_argument("--window", type=float, default=4.0, metavar="SECONDS")
    parser.add_argument("--step", type=float, default=10.0, metavar="SECONDS")
    parser.set_defaults(handler=_drift)


def _drift(args: argparse.Namespace, config: Config) -> int:
    reference, other = _decode_pair(args, config)
    limit = None
    if args.start is not None or args.end is not None:
        limit = (args.start or 0.0, args.end or reference.duration_s)
    series = measure_drift(
        reference.analysis(), other.analysis(), args.base_ms / 1000.0,
        sr=reference.analysis_rate, window_s=args.window, step_s=args.step, limit=limit,
    ).confident()
    for point in series:
        print(f"{point.t:9.2f}  {point.lag_ms:+10.3f} ms  r={point.r:.3f}")
    if len(series) < 2:
        print("not enough confident points to fit a rate")
        return 1
    print(fit_rate(series).describe())
    return 0


# ----------------------------------------------------------------- changepoints
def _register_changepoints(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "changepoints", help="find the jumps rather than reading them off a printout"
    )
    add_pair_arguments(parser)
    parser.add_argument("--window", type=float, default=8.0, metavar="SECONDS")
    parser.add_argument("--step", type=float, default=4.0, metavar="SECONDS")
    parser.add_argument("--penalty", type=float, default=None,
                        help="the cost of one more boundary; derived from the noise if absent")
    parser.add_argument("--no-refine", action="store_true",
                        help="skip pinning each jump down to a few tens of milliseconds")
    parser.set_defaults(handler=_changepoints)


def _segmentation(args: argparse.Namespace, config: Config) -> tuple[Decoded, Decoded, object]:
    reference, other = _decode_pair(args, config)
    series = dense_map(
        reference.analysis(), other.analysis(), sr=reference.analysis_rate,
        window_s=args.window, step_s=args.step,
    ).confident()
    found = find_changepoints(series, penalty=args.penalty)
    if not args.no_refine:
        found = refine_changepoints(
            found, reference.analysis(), other.analysis(), sr=reference.analysis_rate
        )
    return reference, other, found


def _changepoints(args: argparse.Namespace, config: Config) -> int:
    _reference, _other, found = _segmentation(args, config)
    for line in found.describe():  # type: ignore[attr-defined]
        print(line)
    return 0 if found.segments else 1  # type: ignore[attr-defined]


# ------------------------------------------------------------------------ plan
def _register_plan(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "plan", help="write the splice plan the measurement implies"
    )
    add_pair_arguments(parser)
    parser.add_argument("--window", type=float, default=8.0, metavar="SECONDS")
    parser.add_argument("--step", type=float, default=4.0, metavar="SECONDS")
    parser.add_argument("--penalty", type=float, default=None)
    parser.add_argument("--no-refine", action="store_true")
    parser.add_argument("--out", type=Path, required=True, metavar="PLAN.toml")
    parser.add_argument("--no-quiet-search", action="store_true",
                        help="put every join exactly on its jump")
    add_write_arguments(parser)
    parser.set_defaults(handler=_plan)


def _plan(args: argparse.Namespace, config: Config) -> int:
    reference, other, found = _segmentation(args, config)
    plan = plan_from_measurements(
        found,  # type: ignore[arg-type]
        sample_rate=reference.sample_rate,
        total_frames=reference.frames,
        other=other.analysis(),
        analysis_rate=reference.analysis_rate,
        source="other",
        reference="reference",
        channels=reference.channels,
        quiet_search=not args.no_quiet_search,
    )
    for line in plan.describe():
        print(line)
    problems = plan.problems()
    for problem in problems:
        print(f"PROBLEM: {problem}")
    if problems:
        return 1
    if not args.apply:
        print(f"would write {args.out} (dry run; pass --apply to write it)")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(plan.to_toml(), encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


# ---------------------------------------------------------------------- splice
def _register_splice(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("splice", help="build the track the plan describes")
    parser.add_argument("plan", type=Path, metavar="PLAN.toml")
    parser.add_argument("other", type=Path, help="the transfer the plan reads from")
    parser.add_argument("--other-stream", type=int, default=0, metavar="N")
    parser.add_argument("--reference", type=Path, default=None,
                        help="the timeline, where a segment reads from it too")
    parser.add_argument("--reference-stream", type=int, default=0, metavar="N")
    parser.add_argument("--work", type=Path, default=Path("./work"))
    parser.add_argument("--out", type=Path, required=True, metavar="OUT.f32le")
    add_write_arguments(parser)
    parser.set_defaults(handler=_splice)


def _splice(args: argparse.Namespace, config: Config) -> int:
    plan = SplicePlan.from_toml(args.plan.read_text(encoding="utf-8"))
    problems = plan.problems()
    for problem in problems:
        print(f"PROBLEM: {problem}")
    if problems:
        return 1
    sources = {
        "other": decode(
            args.other, out_dir=args.work, audio_index=args.other_stream,
            name="other", config=config,
        ).full()
    }
    if args.reference is not None:
        sources["reference"] = decode(
            args.reference, out_dir=args.work, audio_index=args.reference_stream,
            name="reference", config=config,
        ).full()
    for line in plan.describe():
        print(line)
    if not args.apply:
        print(f"would write {args.out} (dry run; pass --apply to write it)")
        return 0
    from .encode import write_raw

    write_raw(splice(plan, sources), args.out)
    print(f"wrote {args.out}")
    return 0


# ---------------------------------------------------------------------- encode
def _register_encode(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("encode", help="write the built samples into a file")
    parser.add_argument("samples", type=Path, metavar="BUILT.f32le")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--codec", choices=sorted(ENCODINGS), default="flac")
    parser.add_argument("--rate", type=int, default=48_000)
    parser.add_argument("--channels", type=int, default=2)
    parser.add_argument("--layout", default=None)
    parser.add_argument("--language", default=None)
    parser.add_argument("--title", default=None)
    parser.add_argument(
        "--no-delay-compensation", action="store_true",
        help="do not trim the encoder's own delay off the head",
    )
    add_write_arguments(parser)
    parser.set_defaults(handler=_encode)


def _encode(args: argparse.Namespace, config: Config) -> int:
    import numpy as np

    block = np.fromfile(args.samples, dtype=np.float32).reshape(-1, args.channels)
    written = encode(
        block, args.out, encoding=args.codec, sample_rate=args.rate,
        layout=args.layout, language=args.language, title=args.title,
        compensate_delay=not args.no_delay_compensation,
        dry_run=not args.apply, config=config,
    )
    print(written.describe())
    print("  " + " ".join(written.command))
    return 0


# ---------------------------------------------------------------------- verify
def _register_verify(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "verify", help="measure the finished result against the reference, edge to edge"
    )
    add_pair_arguments(parser)
    parser.add_argument("--window", type=float, default=4.0, metavar="SECONDS")
    parser.add_argument("--step", type=float, default=5.0, metavar="SECONDS")
    parser.add_argument("--bar-ms", type=float, default=BAR_MS)
    parser.set_defaults(handler=_verify)


def _verify(args: argparse.Namespace, config: Config) -> int:
    reference, other = _decode_pair(args, config)
    report = verify_against(
        reference.full(), other.full(), sample_rate=reference.sample_rate,
        window_s=args.window, step_s=args.step, bar_ms=args.bar_ms,
    )
    for line in report.describe():
        print(line)
    return 0 if report.passed else 1


# -------------------------------------------------------------------- controls
def _register_controls(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "controls", help="the known-answer harness, run before anything is trusted"
    )
    parser.add_argument("--work", type=Path, default=None,
                        help="also run the controls through the real programs")
    parser.set_defaults(handler=_controls)


def _controls(args: argparse.Namespace, config: Config) -> int:
    report = array_controls()
    for line in report.describe():
        print(line)
    passed = report.passed
    if args.work is not None:
        through_files = file_controls(args.work, config=config)
        for line in through_files.describe():
            print(line)
        passed = passed and through_files.passed
    return 0 if passed else 1


REGISTRY: dict[str, SubCommand] = {
    "probe": _register_probe,
    "decode": _register_decode,
    "map": _register_map,
    "drift": _register_drift,
    "changepoints": _register_changepoints,
    "plan": _register_plan,
    "splice": _register_splice,
    "encode": _register_encode,
    "verify": _register_verify,
    "controls": _register_controls,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dubalign", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"dubalign {__version__}")
    add_common_arguments(parser)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for register in REGISTRY.values():
        register(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    return run(build_parser(), argv)
