"""Service control: the default asks, the context manager always restarts.

Nothing here starts or stops anything. What is being tested is the promise the
protocol makes to its callers, which is the part a database-surgery routine
depends on.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import subprocess

import pytest
from jfkit.service import (
    ManualServiceController,
    ServiceControlError,
    ServiceController,
    SystemdServiceController,
    WindowsServiceController,
    controller_for,
    stopped,
)


def _always(answer: bool) -> ManualServiceController:
    return ManualServiceController(name="the example service", confirm=lambda _: answer)


def test_the_default_controller_is_the_one_that_asks() -> None:
    controller = controller_for()
    assert isinstance(controller, ManualServiceController)


def test_every_implementation_satisfies_the_protocol() -> None:
    for controller in (
        ManualServiceController(),
        SystemdServiceController(name="example"),
    ):
        assert isinstance(controller, ServiceController)


def test_confirmation_moves_the_believed_state() -> None:
    controller = _always(True)
    assert controller.is_running()
    controller.stop()
    assert not controller.is_running()
    controller.start()
    assert controller.is_running()


def test_a_refused_confirmation_is_an_error_not_a_shrug() -> None:
    controller = _always(False)
    with pytest.raises(ServiceControlError):
        controller.stop()


def test_the_context_manager_restarts_after_a_failure() -> None:
    controller = _always(True)
    with pytest.raises(ZeroDivisionError):
        with stopped(controller):
            assert not controller.is_running()
            raise ZeroDivisionError("surgery went wrong")
    assert controller.is_running(), "the service must come back even on failure"


def test_a_service_that_was_already_down_stays_down() -> None:
    controller = _always(True)
    controller.stop()
    with stopped(controller):
        pass
    assert not controller.is_running()


def test_the_unit_manager_implementation_refuses_clearly() -> None:
    controller = SystemdServiceController(name="example")
    for call in (controller.is_running, controller.stop, controller.start):
        with pytest.raises(ServiceControlError) as caught:
            call()
        assert "manual" in str(caught.value)


def test_an_unknown_kind_is_an_error() -> None:
    with pytest.raises(ServiceControlError):
        controller_for("systemd-but-spelled-wrong")


@pytest.mark.parametrize("kind", ["windows", "systemd"])
def test_a_real_controller_needs_the_services_registered_name(kind: str) -> None:
    """There is no default name: a guessed one addresses a service nobody has."""
    with pytest.raises(ServiceControlError, match="--service-name"):
        controller_for(kind)


def test_the_name_given_is_the_name_used() -> None:
    controller = controller_for("systemd", name="example-media")
    assert isinstance(controller, SystemdServiceController)
    assert controller.name == "example-media"
    assert controller_for(name="example-media").name == "example-media"


def _answering(
    controller: WindowsServiceController, stdout: str,
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def fake_run(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(list(args), 0, stdout, "")

    monkeypatch.setattr(controller, "_run", fake_run)
    return calls


def test_the_service_control_program_is_asked_with_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = WindowsServiceController(name="example-media")
    calls = _answering(
        controller,
        "SERVICE_NAME: example-media\n        STATE              : 4  RUNNING\n",
        monkeypatch,
    )
    assert controller.is_running()
    assert calls == [("query", "example-media")]


def test_nssm_is_asked_with_status_and_its_answer_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NSSM has no query verb; its status answer is one word, sometimes wide."""
    controller = WindowsServiceController(name="example-media", program="nssm.exe")
    wide = "\x00".join("SERVICE_STOPPED") + "\x00\r\n"
    calls = _answering(controller, wide, monkeypatch)
    assert not controller.is_running()
    assert calls == [("status", "example-media")]
