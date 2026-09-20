"""jfkit.dto -- one DTO round-trip helper, replacing a folder of per-title scripts.

Reading an item, changing two fields and posting it back is not as simple as
it sounds, and getting it wrong nulls data silently:

  * fetch the FULL single-item DTO from the user-scoped route; the unscoped
    one answers 400 on this server version,
  * strip the trickplay block or the post fails with a server error,
  * post the WHOLE object back: omitted scalars are nulled, not left alone,
  * phase order, enforced: numbers first, then a non-replacing refresh, then
    names and overviews, then dates LAST (a refresh re-seeds dates from the
    container's creation time), then the metadata lock, and never on a folder,
    series or season, where it cascades to every recursive child.

The copies-vs-discards table is data with a test behind it, not a comment.

Planned public API:
    update_item(client, item_id, fields, *, phase_order=True) -> None
    COPIES: frozenset[str]   # fields UpdateItem keeps
    DISCARDS: frozenset[str] # fields UpdateItem drops outright

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
