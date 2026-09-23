"""mkvkit.run -- one place that knows how to call an external program.

Every module in this package that shells out does it through a
:class:`Runner`. That is worth a module of its own for three reasons.

**Encoding is decided once.** Every call is explicit UTF-8 with replacement,
so a track named in a script this machine has no font for cannot turn a probe
into a decoding error two frames deep.

**A tolerated exit code is declared, not guessed.** The muxer exits 1 for
warnings, and a warning is routine -- a file with a non-fatal oddity still
identifies fine. Every caller says which codes it accepts, so "it exited 1"
never silently becomes either a crash or a pass.

**Tests do not need the programs.** A runner is a callable, so a test supplies
one that returns recorded text and the module under test never learns the
difference. That is what makes the parsing, the comparison and the command
building testable on a machine with nothing installed -- which is most of the
code in this package.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import Config
from .tools import find_tool

__all__ = [
    "CommandFailed",
    "Result",
    "Runner",
    "SubprocessRunner",
    "default_runner",
]

log = logging.getLogger(__name__)

#: How much of a failing program's output an error message carries.
ERROR_TAIL_LINES = 12


@dataclass(frozen=True)
class Result:
    """What a program said, kept whole so a caller can report it."""

    tool: str
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def tail(self) -> str:
        """The last lines of the output, for an error message."""
        text = self.stderr.strip() or self.stdout.strip()
        return "\n".join(text.splitlines()[-ERROR_TAIL_LINES:])


class CommandFailed(RuntimeError):
    """A program exited with a code its caller did not accept."""

    def __init__(self, result: Result) -> None:
        self.result = result
        super().__init__(
            f"{result.tool} exited {result.returncode}\n"
            f"  {' '.join(result.argv)}\n{result.tail}"
        )


class Runner(Protocol):
    """Run one external program and return what it said.

    ``ok`` lists the exit codes the caller accepts; anything else raises
    :class:`CommandFailed`.
    """

    def __call__(
        self, tool: str, args: Sequence[str | Path], *, ok: Sequence[int] = (0,)
    ) -> Result: ...


class SubprocessRunner:
    """The real thing: resolve the program once, then run it."""

    def __init__(self, config: Config | None = None, *, timeout: float | None = None):
        self._config = config
        self._timeout = timeout

    def __call__(
        self, tool: str, args: Sequence[str | Path], *, ok: Sequence[int] = (0,)
    ) -> Result:
        executable = find_tool(tool, config=self._config)
        argv = [str(executable), *(str(a) for a in args)]
        log.debug("run %s", " ".join(argv))
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self._timeout,
            check=False,
        )
        result = Result(
            tool=tool,
            argv=tuple(argv),
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
        if result.returncode not in tuple(ok):
            raise CommandFailed(result)
        return result


def default_runner(config: Config | None = None) -> Runner:
    """The runner every public function falls back to when given none."""
    return SubprocessRunner(config)
