"""mkvkit.langid -- identify the spoken language of an audio track.

The package is in three layers, and they are kept apart on purpose:

``worker``  reads audio and records what a detector said, per window;
``ladder``  decides what a set of windows means, as pure arithmetic;
``priors``  turns context into evidence that can only ever lower a bar;
``review``  applies the rules to collected evidence and queues what is left;
``report``  says what was found, and what is still open;
``sources`` produces the list of tracks to look at -- from a directory by
default, from a catalogue when one is available.

Evidence is collected once and can be decided many times. Changing a
threshold then costs a second rather than another pass over the library --
which is the difference between a tuned constant and a constant nobody dares
to touch.

The thresholds in :class:`~mkvkit.langid.ladder.SettleBar` are fitted
defaults, not universals. They are configuration, and the method for re-fitting
them ships with them.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from .ladder import (
    Decision,
    Posterior,
    SettleBar,
    StageBar,
    Support,
    Verdict,
    aggregate,
    mixed_split,
    settle,
    trimmed_aggregate,
)
from .priors import (
    Prior,
    SiblingVote,
    blocking_reasons,
    combine,
    release_token_prior,
    sibling_prior,
)
from .report import Summary, render_markdown, render_tsv, summarise
from .review import Outcome, Rescan, RescanReason, decide, decide_all, rescan_queue
from .sources import (
    CatalogueSource,
    FilesystemSource,
    Job,
    JobSource,
    device_hint,
    read_jobs,
    write_jobs,
)
from .worker import (
    DEFAULT_OFFSETS,
    RETRY_OFFSETS,
    SAMPLE_RATE,
    Detector,
    Extractor,
    FfmpegExtractor,
    ResultLog,
    SpeechGate,
    TrackEvidence,
    WhisperDetector,
    Window,
    WindowResult,
    scan_track,
    window_starts,
)

__all__ = [
    "DEFAULT_OFFSETS",
    "RETRY_OFFSETS",
    "SAMPLE_RATE",
    "CatalogueSource",
    "Decision",
    "Detector",
    "Extractor",
    "FfmpegExtractor",
    "FilesystemSource",
    "Job",
    "JobSource",
    "Outcome",
    "Posterior",
    "Prior",
    "Rescan",
    "RescanReason",
    "ResultLog",
    "SettleBar",
    "SiblingVote",
    "SpeechGate",
    "StageBar",
    "Summary",
    "Support",
    "TrackEvidence",
    "Verdict",
    "WhisperDetector",
    "Window",
    "WindowResult",
    "aggregate",
    "blocking_reasons",
    "combine",
    "decide",
    "decide_all",
    "device_hint",
    "mixed_split",
    "read_jobs",
    "release_token_prior",
    "render_markdown",
    "render_tsv",
    "rescan_queue",
    "scan_track",
    "settle",
    "sibling_prior",
    "summarise",
    "trimmed_aggregate",
    "window_starts",
    "write_jobs",
]
