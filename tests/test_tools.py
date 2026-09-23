"""Tool discovery: the order, the cache, and the error that says where it looked.

No real media program is needed for any of this. The interpreter running the
suite stands in as an external program, because what is being tested is the
resolution order, not ffmpeg.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import MappingProxyType

import pytest
from mkvkit.config import Config, ToolsConfig
from mkvkit.tools import (
    ENV_PREFIX,
    KNOWN_TOOLS,
    ToolNotFound,
    clear_cache,
    find_tool,
    search_locations,
    tool_version,
)

INTERPRETER = Path(sys.executable)


@pytest.fixture(autouse=True)
def _clear() -> None:
    clear_cache()


def _config(**entries: str) -> Config:
    return Config(tools=ToolsConfig(MappingProxyType(dict(entries))))


def test_the_config_entry_wins() -> None:
    found = find_tool("ffmpeg", config=_config(ffmpeg=str(INTERPRETER)), environ={})
    assert found == INTERPRETER.resolve()


def test_the_environment_beats_the_path() -> None:
    environ = {f"{ENV_PREFIX}FFPROBE": str(INTERPRETER), "PATH": ""}
    assert find_tool("ffprobe", environ=environ) == INTERPRETER.resolve()


def test_the_path_is_searched(tmp_path: Path) -> None:
    name = "mkvmerge.exe" if os.name == "nt" else "mkvmerge"
    stand_in = tmp_path / name
    stand_in.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stand_in.chmod(0o755)
    found = find_tool("mkvmerge", environ={"PATH": str(tmp_path)})
    assert found == stand_in.resolve()


def test_a_missing_program_names_everywhere_it_looked(tmp_path: Path) -> None:
    with pytest.raises(ToolNotFound) as caught:
        find_tool("mkvpropedit", environ={"PATH": str(tmp_path / "empty")})
    error = caught.value
    assert error.tool == "mkvpropedit"
    assert "PATH" in error.tried
    message = str(error)
    assert "[tools].mkvpropedit" in message
    assert f"{ENV_PREFIX}MKVPROPEDIT" in message


def test_a_resolution_is_cached() -> None:
    config = _config(ffmpeg=str(INTERPRETER))
    first = find_tool("ffmpeg", config=config, environ={})
    second = find_tool("ffmpeg", config=config, environ={})
    assert first == second
    clear_cache()
    assert find_tool("ffmpeg", config=config, environ={}) == first


def test_search_locations_only_returns_directories_that_exist() -> None:
    for directory in search_locations("ffmpeg"):
        assert directory.is_dir()


def test_tool_version_reads_a_line() -> None:
    assert tool_version(INTERPRETER).strip()


def test_tool_version_never_raises(tmp_path: Path) -> None:
    assert tool_version(tmp_path / "not-a-program") == "version unknown"


def test_the_known_tools_are_the_ones_the_config_accepts() -> None:
    assert set(KNOWN_TOOLS) == {
        "ffmpeg", "ffprobe", "mkvmerge", "mkvpropedit", "mkvextract",
    }
