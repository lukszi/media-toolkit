"""jfkit.service -- service control behind a protocol, never behind an assumption.

Some operations have to happen with the server stopped: surgery on its
database, restoring an options file it caches in memory, swapping a file it
holds open. Nothing in this library decides on its own that it may stop
somebody's server.

So control is a protocol with three implementations, and the manual one is the
default: it prints exactly what to do and waits to be told it was done. On a
machine where the service is not ours to touch, that is the only correct
behaviour, and it keeps the calling code identical everywhere.

Use :func:`stopped` rather than calling stop and start by hand. It restarts
the service even when the body raises, which is the failure that matters: an
exception during database surgery must not leave the service down.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "ManualServiceController",
    "ServiceControlError",
    "ServiceController",
    "SystemdServiceController",
    "WindowsServiceController",
    "controller_for",
    "stopped",
]

log = logging.getLogger(__name__)

POLL_SECONDS = 2.0
DEFAULT_TIMEOUT = 120.0


class ServiceControlError(RuntimeError):
    """The service did not reach the state that was asked for."""


@runtime_checkable
class ServiceController(Protocol):
    """Start and stop one service, whatever runs it."""

    name: str

    def is_running(self) -> bool: ...

    def stop(self, *, timeout: float = DEFAULT_TIMEOUT) -> None: ...

    def start(self, *, timeout: float = DEFAULT_TIMEOUT) -> None: ...


@contextmanager
def stopped(controller: ServiceController) -> Iterator[None]:
    """Stop the service for the body, and start it again whatever happens.

    A service that was already down is left down: the caller stopped it for a
    reason that is none of this function's business.
    """
    was_running = controller.is_running()
    if was_running:
        controller.stop()
    try:
        yield
    finally:
        if was_running:
            controller.start()


# ------------------------------------------------------------------- manual
@dataclass
class ManualServiceController:
    """Print what to do and wait. The default, and the only safe assumption.

    ``confirm`` is injected so tests -- and a detached run that must not block
    on a terminal -- supply their own answer instead of reading standard input.
    """

    name: str = "the media server"
    confirm: Callable[[str], bool] = field(default=lambda prompt: input(prompt) == "")
    _believed_running: bool = field(default=True, init=False, repr=False)

    def is_running(self) -> bool:
        # It cannot be known from here, so assume the state that makes the
        # caller ask before it touches anything.
        return self._believed_running

    def stop(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        if not self.confirm(f"Stop {self.name}, then press Enter: "):
            raise ServiceControlError(f"{self.name} was not confirmed stopped")
        self._believed_running = False
        log.info("%s: reported stopped by the operator", self.name)

    def start(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        if not self.confirm(f"Start {self.name} again, then press Enter: "):
            raise ServiceControlError(f"{self.name} was not confirmed started")
        self._believed_running = True
        log.info("%s: reported started by the operator", self.name)


# ------------------------------------------------------------------ windows
@dataclass
class WindowsServiceController:
    """Drive a Windows service through the service-control program, or through NSSM.

    Both take the same three verbs in the same argument position, so one
    implementation covers a service registered either way: pass the NSSM
    executable as ``program`` when the service was installed with it.

    State is read from the control program rather than inferred from the exit
    code of the stop verb, which returns as soon as the request is accepted --
    not when the service has finished shutting down. Polling until the state
    actually changes is the difference between a clean database file and a
    half-written one.
    """

    name: str
    program: str = "sc"

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        executable = shutil.which(self.program)
        if executable is None:
            raise ServiceControlError(
                f"{self.program} was not found; service control is not available here"
            )
        return subprocess.run(
            [executable, *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )

    def _state(self) -> str:
        completed = self._run("query", self.name)
        if completed.returncode != 0:
            raise ServiceControlError(
                f"cannot read the state of {self.name} (exit {completed.returncode})"
            )
        for line in completed.stdout.splitlines():
            if "STATE" in line.upper():
                return line.split(":")[-1].strip().split()[-1].upper()
        raise ServiceControlError(f"no state reported for {self.name}")

    def is_running(self) -> bool:
        return self._state() == "RUNNING"

    def _await(self, wanted: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._state() == wanted:
                return
            time.sleep(POLL_SECONDS)
        raise ServiceControlError(f"{self.name} did not reach {wanted} in {timeout:.0f}s")

    def stop(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._run("stop", self.name)
        self._await("STOPPED", timeout)
        log.info("%s: stopped", self.name)

    def start(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._run("start", self.name)
        self._await("RUNNING", timeout)
        log.info("%s: started", self.name)


# ------------------------------------------------------------------ systemd
@dataclass
class SystemdServiceController:
    """The unit-manager implementation.

    Deliberately a stub. The command shapes are not the hard part; knowing
    that they were exercised is, and there is no integration test driving a
    real unit yet. An implementation nobody has run against a live service is
    worse than an honest refusal, because the caller finds out during database
    surgery.

    Until that test exists, use :class:`ManualServiceController`, which works
    on every platform and asks first.
    """

    name: str
    user: bool = False

    def _unsupported(self, verb: str) -> ServiceControlError:
        return ServiceControlError(
            f"{verb} through the unit manager is not implemented yet; "
            f"use the manual controller, or stop {self.name} by hand"
        )

    def is_running(self) -> bool:
        raise self._unsupported("reading the state")

    def stop(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        raise self._unsupported("stopping")

    def start(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        raise self._unsupported("starting")


def controller_for(kind: str = "manual", *, name: str = "the media server") -> ServiceController:
    """Pick an implementation by name. ``manual`` is the default, on purpose."""
    if kind == "manual":
        return ManualServiceController(name=name)
    if kind == "windows":
        if sys.platform != "win32":
            raise ServiceControlError("the Windows controller needs a Windows host")
        return WindowsServiceController(name=name)
    if kind == "systemd":
        return SystemdServiceController(name=name)
    raise ServiceControlError(f"unknown service controller {kind!r}")
