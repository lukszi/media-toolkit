"""Stand-ins for the payload check, for tests whose files are not media.

Parking, copying, chunking and restoring are tested on a few bytes of text,
because those tests are about the bytes moving, not about what they encode.
The payload check (:mod:`mkvkit.integrity`) would rightly refuse such a file,
so those tests replace it -- explicitly, by name, with this -- and the tests
of the check itself run the real one on real media.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mkvkit import integrity
from mkvkit.integrity import IntegrityReport


def plays(path: Path | str, **_options: Any) -> IntegrityReport:
    """A report that says the payload is there, and says it is a stand-in."""
    return IntegrityReport(
        path=Path(path), notes=("a stand-in: this test is not about the payload",)
    )


def payload_is_a_stand_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every default payload check in this test answers with :func:`plays`."""
    monkeypatch.setattr(integrity, "check", plays)
