"""Shared test plumbing: marker gating and the generated media fixtures.

A test that needs an external program says so with a marker. If the program is
not installed the test skips rather than errors, so a partial toolchain gives
a report that still means something.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures import build, ffmpeg_missing
from tests.fixtures.make_fixtures import locate

#: The checkout this file sits in. Outside one -- the test suite run against an
#: installed distribution -- the files a ``repository`` test names are absent.
REPO_ROOT = Path(__file__).resolve().parents[1]

#: marker -> the programs it needs, found the way the toolkit finds them
#: (configuration, environment, PATH, then the platform install directories)
MARKER_TOOLS = {
    "needs_ffmpeg": ("ffmpeg", "ffprobe"),
    "needs_mkvtoolnix": ("mkvmerge", "mkvpropedit"),
}


def pytest_configure(config: pytest.Config) -> None:
    # Also declared in the root pyproject.toml; repeated here because a run
    # outside a checkout does not have that file, and would warn about every
    # one of them.
    for marker in (
        "repository(*paths): checks the checkout itself; "
        "skipped when the named files are absent",
        "needs_ffmpeg: requires ffmpeg and ffprobe",
        "needs_mkvtoolnix: requires the MKVToolNix command-line tools",
        "needs_asr: requires a speech model and is slow",
        "slow: takes more than a few seconds",
    ):
        config.addinivalue_line("markers", marker)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    for item in items:
        for marker, programs in MARKER_TOOLS.items():
            if marker not in item.keywords:
                continue
            absent = [p for p in programs if locate(p) is None]
            if absent:
                item.add_marker(
                    pytest.mark.skip(reason=f"not installed: {', '.join(absent)}")
                )
        # A test about the repository -- its licences, its changelogs, its
        # example file, its tree -- names the files it reads. Where they are
        # missing this is not a checkout, and the test has nothing to check.
        for mark in item.iter_markers("repository"):
            missing = [p for p in mark.args if not (REPO_ROOT / p).exists()]
            if missing:
                item.add_marker(pytest.mark.skip(
                    reason=f"not run from a checkout: {', '.join(missing)} absent"
                ))


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


@pytest.fixture(autouse=True)
def _rollbacks_stay_in_the_test(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An applied edit writes its rollback before it edits; not into the checkout.

    The default location is ``<[paths].work>/rollback`` relative to wherever
    the run starts, which in a test run is the repository. A test that wants
    the real default imports the function and calls it directly.
    """
    from mkvkit import propedit

    where = tmp_path_factory.mktemp("rollback")
    monkeypatch.setattr(propedit, "default_rollback_dir", lambda _config: where)
