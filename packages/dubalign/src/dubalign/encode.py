"""dubalign.encode -- write the built samples out, and account for the encoder.

Two things about this step are worth code rather than a note.

**An encoder has a delay of its own.** At least one common one holds back 256
samples, which is 5.333 ms at 48 kHz -- a small number that is nevertheless a
constant error across the whole programme, applied after every measurement in
this package has already said the build is exact. The first time it was met,
the result measured +5.333 ms at every test point and every one of those
points was correct: the build was right and the file was late. The fix is to
trim exactly that many samples off the head, out of leading silence where there
is any, and it is applied here by name rather than discovered again.

**A lossless carrier and a compatible one.** The lossless output is the
deliverable; the lossy one exists because players differ in what they will
play directly, and a track a player has to transcode is a track that arrives
late on some of them. Both are written from the same samples in the same call,
so they cannot drift apart.

Nothing here decides what the output is called or where it goes: those are
arguments. The one-off scripts this grew out of had both baked in, which is
why they could only ever build the one programme they were written for.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from mkvkit.config import Config
from mkvkit.run import Runner, default_runner
from numpy.typing import NDArray

from .align import FULL_RATE

__all__ = [
    "ENCODER_DELAY_SAMPLES",
    "ENCODINGS",
    "Encoded",
    "Encoding",
    "encode",
    "encode_command",
    "write_raw",
]

log = logging.getLogger(__name__)

#: How many samples the lossy encoder holds back before its first output. A
#: constant of that encoder, not of this programme, which is why it is here.
ENCODER_DELAY_SAMPLES = {"ac3": 256, "eac3": 256}


@dataclass(frozen=True)
class Encoding:
    """One output format, and the arguments that produce it."""

    name: str
    suffix: str
    args: tuple[str, ...]
    lossless: bool


ENCODINGS: dict[str, Encoding] = {
    "flac": Encoding(
        name="flac",
        suffix=".flac",
        args=(
            "-af", "aformat=sample_fmts=s32", "-c:a", "flac",
            "-compression_level", "8", "-bits_per_raw_sample", "24",
        ),
        lossless=True,
    ),
    "ac3": Encoding(
        name="ac3", suffix=".ac3", args=("-c:a", "ac3", "-b:a", "640k"), lossless=False
    ),
}


@dataclass(frozen=True)
class Encoded:
    """What was written, and what was done to it on the way out."""

    path: Path
    encoding: str
    trimmed_samples: int
    command: tuple[str, ...]

    def describe(self) -> str:
        trim = (
            "" if not self.trimmed_samples
            else f", {self.trimmed_samples} sample(s) trimmed for the encoder's delay"
        )
        return f"{self.path.name}: {self.encoding}{trim}"


def write_raw(
    samples: NDArray[np.floating], path: Path | str, *, dtype: str = "float32"
) -> Path:
    """Write the built block as headerless samples, which is what is encoded from."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    np.ascontiguousarray(np.asarray(samples, dtype=dtype)).tofile(target)
    return target


def encode_command(
    raw: Path | str,
    out: Path | str,
    encoding: Encoding,
    *,
    sample_rate: int = FULL_RATE,
    channels: int = 2,
    layout: str | None = None,
    language: str | None = None,
    title: str | None = None,
    trim_samples: int = 0,
) -> list[str]:
    """The arguments for one output. Built separately so it can be read in a test."""
    args = [
        "-nostdin", "-v", "error", "-y",
        "-f", "f32le", "-ar", str(sample_rate), "-ac", str(channels),
    ]
    if layout:
        args += ["-channel_layout", layout]
    args += ["-i", str(raw)]
    if trim_samples:
        # Out of the head, where a built track normally has silence to spare.
        args += ["-af", f"atrim=start_sample={trim_samples}"]
    args += list(encoding.args)
    if language:
        args += ["-metadata:s:a:0", f"language={language}"]
    if title:
        args += ["-metadata:s:a:0", f"title={title}"]
    args.append(str(out))
    return args


def encode(
    samples: NDArray[np.floating],
    out: Path | str,
    *,
    encoding: str = "flac",
    sample_rate: int = FULL_RATE,
    layout: str | None = None,
    language: str | None = None,
    title: str | None = None,
    work_dir: Path | str | None = None,
    compensate_delay: bool = True,
    dry_run: bool = True,
    runner: Runner | None = None,
    config: Config | None = None,
) -> Encoded:
    """Write one output from the built samples.

    ``dry_run`` is the default: the command is built, logged and returned, and
    nothing is written. Every writing entry point in this toolkit behaves the
    same way and there is no third state.
    """
    chosen = ENCODINGS.get(encoding)
    if chosen is None:
        raise ValueError(
            f"{encoding}: not one of {', '.join(sorted(ENCODINGS))}"
        )
    block = np.asarray(samples)
    channels = block.shape[1] if block.ndim > 1 else 1
    trim = ENCODER_DELAY_SAMPLES.get(encoding, 0) if compensate_delay else 0
    target = Path(out)
    work = Path(work_dir) if work_dir is not None else target.parent

    def command_for(raw: Path) -> list[str]:
        return encode_command(
            raw, target, chosen, sample_rate=sample_rate, channels=channels,
            layout=layout, language=language, title=title, trim_samples=trim,
        )

    if dry_run:
        # The real intermediate gets a name of its own when there is one to
        # write; this stands in for it in the command that is shown.
        placeholder = work / f".{target.stem}.XXXXXXXX.f32le"
        result = Encoded(
            path=target, encoding=encoding, trimmed_samples=trim,
            command=tuple(command_for(placeholder)),
        )
        log.info("would write %s", result.describe())
        return result
    # The intermediate is a file this call creates under a name nobody else
    # chose, and it is the only thing this call removes. Deriving it from the
    # output's name once overwrote and then deleted the caller's own samples
    # whenever they shared a stem with the output.
    work.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=f".{target.stem}.", suffix=".f32le", dir=work)
    os.close(handle)
    raw = Path(name)
    command = command_for(raw)
    result = Encoded(
        path=target, encoding=encoding, trimmed_samples=trim, command=tuple(command)
    )
    try:
        write_raw(block, raw)
        run = runner if runner is not None else default_runner(config)
        run("ffmpeg", command)
    finally:
        raw.unlink(missing_ok=True)
    log.info("wrote %s", result.describe())
    return result
