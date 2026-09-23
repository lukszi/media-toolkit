"""mkvkit.chapters.verify -- matching marks is not the same as matching names.

The finding this module exists for: a published chapter set whose timestamp
grid matches your cut to within a couple of seconds proves that the MARKS fit.
It says nothing about whether the NAMES were typed against those marks: a
grid can match perfectly while the names describe a scene a mark or more
away.

So names are verified against the content. Twenty seconds of original-language
audio from each mark is transcribed and one frame is taken shortly after it --
both out of a SINGLE seek, because seeking twice per mark is what makes this
too slow to run. Then the content-word hit rate of name i is scored against
transcript i+k for a range of k: a clearly better score at k other than zero
is the misalignment signature. The absolute score is weak evidence and is
reported as weak evidence.

Only a set that comes out aligned is eligible to be written. Uncertain is a
verdict, not a rounding error, and a confidently wrong name is worse than no
name at all.

The half of this that is arithmetic now exists elsewhere: matching two grids,
in both rate directions, and the rule that a rate-converted candidate may only
have its names copied onto marks a file already has, live in
:mod:`mkvkit.chapters.grid`. What is left here is the half that needs the
content -- audio, frames, a transcriber -- and it is the half the finding is
about.

Planned public API:
    collect_evidence(media, marks, *, audio_stream, transcriber) -> list[MarkEvidence]
    shift_score(names, transcripts, *, shifts=range(-4, 5)) -> ShiftScores
    verdict(evidence, shifts, *, rubric=DEFAULT_RUBRIC) -> Verdict

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
