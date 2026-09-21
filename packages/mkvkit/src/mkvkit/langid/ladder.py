"""mkvkit.langid.ladder -- the escalation ladder and the settle bar.

Five stages: dense activity-gated sampling, a whole-track speech hunt that
can also conclude 'no dialogue at all', a transcription cross-check on script
and function words, an independent model FAMILY, and finally a clip for a
person to listen to.

Aggregation sums log-probabilities and reports the geometric mean, so one
strongly disagreeing window collapses the score. That is intended: a track
that reads as two languages should not settle.

The bar is asymmetric on purpose. Confirming an existing tag is cheap;
overturning one must clear a higher confidence, more counted windows and a
higher agreement fraction, because an overturn rewrites metadata a person
chose.

Every constant here is config-loadable, and every default is a fitted one.
They are defaults, not universals, and the
method used to fit them ships with them.

Planned public API:
    aggregate(evidence, *, trim_frac=0.0) -> Posterior
    settle(post, *, existing_tag, priors, bar) -> Verdict
    @dataclass(frozen=True) SettleBar
    review(track, *, stages=(1,2,3,4,5), bar, priors, detectors) -> Review

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
