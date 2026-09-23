# Changelog -- jfkit

This file records what changed and why it is safe to install. Dates are the
day the work was done.

## 0.1.0 -- 2026-09-23

First release. What is here is finished and tested; what is not here is
skeletons, and they say so in their own docstrings.

### Added

- **`jfkit.naming`** -- predicts how a media server will read a filename,
  before anything is renamed. A port of the episode-path expressions of one
  release, pinned by a table of sixty-five invented names, with the version it
  was checked against recorded in the module. The two shapes the ported
  expressions cannot claim are reported as such rather than as "no match".
- **`jfkit.client`** -- a standard-library HTTP client. The token belongs to
  the client object, is resolved from the configured indirection when it is
  first needed, and is registered with the redaction filter in the same step,
  so it cannot reach a log. Item queries are **user-scoped by default**,
  because the unscoped collection route omits items rather than failing.
  Writes are opt-in: a client is constructed in dry-run mode, where a mutating
  request is logged in full and not sent. Timeouts and a bounded retry, and
  only idempotent requests are retried.
- **`jfkit.client.wait_idle`** -- refuse to start while somebody is
  mid-episode. Every destructive pipeline should call it.
- **`jfkit naming`** on the command line, with an exit code that means
  something: non-zero when any name would be read as an episode range.
- **`docs/gotchas/jellyfin-12.md`** -- the server reference, grouped and
  version-stamped, with the three entries that look like upstream bugs marked
  as such.

### Known limits

- Validated against one server release. Every entry in the gotcha reference
  carries the version it was confirmed on, and the filename predictor names
  the release it was read from; neither is a claim about any other release.
- `swap`, `refresh`, `dto`, `libopts`, `maintenance`, `surveys`, `segments`,
  `safedelete` and `jobs` are skeletons. They land at 0.3.0.
- The client covers the routes this release needs and no more: items,
  sessions, and whatever a caller asks for by path.
