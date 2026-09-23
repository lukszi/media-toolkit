"""mkvkit.langid -- identify the spoken language of an audio track.

The package is in three layers, and they are kept apart on purpose:

``worker``  reads audio and records what a detector said, per window;
``ladder``  decides what a set of windows means, as pure arithmetic;
``report``  says what was found, and what is still open.

Evidence is collected once and can be decided many times. Changing a
threshold then costs a second rather than another pass over the library --
which is the difference between a tuned constant and a constant nobody dares
to touch.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

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
    "Detector",
    "Extractor",
    "FfmpegExtractor",
    "ResultLog",
    "SpeechGate",
    "TrackEvidence",
    "WhisperDetector",
    "Window",
    "WindowResult",
    "scan_track",
    "window_starts",
]
