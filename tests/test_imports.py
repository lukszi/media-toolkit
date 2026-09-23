"""Every module in every package imports, on its own, with nothing installed.

A skeleton that does not import is not a skeleton, it is a broken package:
an import error only shows up when somebody installs the distribution, which
is the worst moment to find out. The packages carry stubs for a while yet, so
this is the cheapest check that keeps the tree honest in the meantime.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("mkvkit", "jfkit", "dubalign")


def _modules() -> list[str]:
    names: list[str] = []
    for package in PACKAGES:
        root = REPO_ROOT / "packages" / package / "src" / package
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(root.parent).with_suffix("")
            parts = list(relative.parts)
            if parts[-1] == "__init__":
                parts.pop()
            names.append(".".join(parts))
    return names


@pytest.mark.parametrize("name", _modules())
def test_module_imports(name: str) -> None:
    module = importlib.import_module(name)
    assert module.__name__ == name


def test_every_package_reports_a_version() -> None:
    for package in PACKAGES:
        module = importlib.import_module(package)
        version = module.__version__
        assert version.startswith("0.0."), f"{package} {version}"


def test_the_entry_points_exist() -> None:
    for package in PACKAGES:
        main = importlib.import_module(f"{package}.cli").main
        assert callable(main)


def test_every_package_ships_its_own_licence() -> None:
    """A distribution built from one package has to be complete on its own."""
    for package in PACKAGES:
        licence = REPO_ROOT / "packages" / package / "LICENSE"
        assert licence.is_file(), licence
        assert "PolyForm Noncommercial License 1.0.0" in licence.read_text(encoding="utf-8")
        init = REPO_ROOT / "packages" / package / "src" / package / "__init__.py"
        spdx = "SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0"
        assert spdx in init.read_text(encoding="utf-8")
