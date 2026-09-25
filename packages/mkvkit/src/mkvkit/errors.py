"""mkvkit.errors -- the error types, so a caller can act on them.

Two rules. A missing external program raises an error naming every location
that was tried; a missing optional dependency raises one naming the exact
install line. Neither is a bare ImportError two frames deep in a helper.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import importlib
from types import ModuleType

from .config import ConfigError, SecretUnavailable
from .tools import ToolNotFound

__all__ = [
    "ConfigError",
    "ExtraRequired",
    "SecretUnavailable",
    "ToolNotFound",
    "require_module",
]


class ExtraRequired(ImportError):
    """An optional dependency is missing, and the message says how to fix it."""

    def __init__(self, module: str, *, extra: str, package: str = "mkvkit") -> None:
        self.module = module
        self.extra = extra
        self.package = package
        super().__init__(
            f"{module} is needed for this and is not installed. "
            "Install the extra from the repository (the packages are not on "
            f'PyPI):  pip install -e "packages/{package}[{extra}]" in a checkout'
        )


def require_module(name: str, *, extra: str, package: str = "mkvkit") -> ModuleType:
    """Import an optional dependency, or raise the actionable error."""
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise ExtraRequired(name, extra=extra, package=package) from exc
