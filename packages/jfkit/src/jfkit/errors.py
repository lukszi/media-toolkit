"""jfkit.errors -- the error types, so a caller can act on them.

The shared ones come from the file-side package: a missing program names every
location that was tried, a missing optional dependency names the exact install
line, and a bad configuration names every problem at once.

The server-side ones are here. They carry the status and the item, because
"the server said no" without either is what turns a two-minute fix into an
afternoon.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from mkvkit.errors import ConfigError, ExtraRequired, SecretUnavailable, ToolNotFound

__all__ = [
    "ConfigError",
    "ExtraRequired",
    "ItemNotFound",
    "SecretUnavailable",
    "ServerRefused",
    "ToolNotFound",
]


class ServerRefused(RuntimeError):
    """The server answered, and the answer was no."""

    def __init__(self, status: int, route: str, detail: str = "") -> None:
        self.status = status
        self.route = route
        self.detail = detail
        suffix = f": {detail}" if detail else ""
        super().__init__(f"{status} from {route}{suffix}")


class ItemNotFound(LookupError):
    """No item with that identifier, in that scope."""

    def __init__(self, item_id: str, scope: str = "") -> None:
        self.item_id = item_id
        self.scope = scope
        where = f" for {scope}" if scope else ""
        super().__init__(f"no item {item_id}{where}")
