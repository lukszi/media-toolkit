"""jfkit.client -- the HTTP client, and the one place a token is resolved.

stdlib urllib only: this is a handful of JSON calls, not a framework.

Two things this fixes about the scripts it comes from. First, the token and
the user id were module-level constants that every other module imported, so
importing a helper pulled in credentials; here they are attributes of a client
object that is passed explicitly. Second, item queries are user-scoped BY
DEFAULT, because the unscoped collection route silently omits items rather
than failing, which is the worst way for a query to be wrong.

wait_idle() belongs here too: every destructive pipeline should refuse to run
while somebody is mid-episode.

Planned public API:
    class Client(config: Config)
    Client.get(path, **params) / .post(path, json=...)
    Client.item(item_id) -> dict            # user-scoped by default
    Client.wait_idle(timeout=3600, poll=15) -> None

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
