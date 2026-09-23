"""Service control: the default asks, the context manager always restarts.

Nothing here starts or stops anything. What is being tested is the promise the
protocol makes to its callers, which is the part a database-surgery routine
depends on.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import pytest
from jfkit.service import (
    ManualServiceController,
    ServiceControlError,
    ServiceController,
    SystemdServiceController,
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
