"""So `python -m tests.fixtures build` works from a checkout."""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import sys

from .make_fixtures import main

if __name__ == "__main__":
    sys.exit(main())
