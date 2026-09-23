"""Shared test plumbing: marker gating and the generated media fixtures.

A test that needs an external program says so with a marker. If the program is
not installed the test skips rather than errors, so a partial toolchain gives
a report that still means something.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures import build, ffmpeg_missing

#: marker -> the programs it needs on the PATH
MARKER_TOOLS = {
    "needs_ffmpeg": ("ffmpeg", "ffprobe"),
    "needs_mkvtoolnix": ("mkvmerge", "mkvpropedit"),
}


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    for item in items:
        for marker, programs in MARKER_TOOLS.items():
            if marker not in item.keywords:
                continue
            absent = [p for p in programs if shutil.which(p) is None]
            if absent:
                item.add_marker(
                    pytest.mark.skip(reason=f"not installed: {', '.join(absent)}")
                )


@pytest.fixture(scope="session")
def media_fixtures() -> dict[str, Path]:
    """Every generated fixture, built once per session and cached on disk."""
    missing = ffmpeg_missing()
    if missing:
        pytest.skip(f"not installed: {missing}")
    return build()


@pytest.fixture(autouse=True)
def _no_leaked_secrets() -> Iterator[None]:
    """No test leaves a registered secret behind for the next one."""
    from mkvkit.logging import clear_secrets

    clear_secrets()
    yield
    clear_secrets()
