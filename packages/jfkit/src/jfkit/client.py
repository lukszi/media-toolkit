"""jfkit.client -- the HTTP client, and the one place a token is resolved.

Standard-library ``urllib`` only. This is a few dozen JSON calls against one
server on a local network, and a request framework would be the largest
dependency in the project for the smallest reason.

Four decisions here are the whole point of the module.

**The token is not a module-level constant.** The scripts this replaces
exported a token and a user id at import time, so importing any helper handed
the importer a credential; the other modules then imported the constants rather
than the functions, and the credential travelled with each of them. Here the
token belongs to a :class:`Client` instance, it is resolved from the
configured indirection at the moment it is first needed, and it is registered
with the logging redaction filter on the way through -- so even an exception
that prints a request header cannot put it in a log file.

**Item queries are user-scoped by default.** The unscoped collection route
omits items rather than failing: it answers, it answers quickly, and the
answer is short. An audit built on it silently skips whatever it skipped. The
user-scoped route returns everything, so it is the default, and asking for the
unscoped one is an explicit act.

**Writes are opt-in.** A client is constructed in dry-run mode. In that state
every mutating request is logged, in full -- body included, with any
credential-looking value in it replaced -- and not sent, so a pipeline can be
run end to end against a real server and produce a complete account of what it
*would* do, which is the only kind of dry run worth having. ``--apply`` builds
a client with ``dry_run=False``. There is no third state.

**Every request has a timeout and a bounded retry.** A server restarting
mid-run should cost a few seconds, not a lost night's work; a server that is
genuinely down should fail while somebody is still watching. Only idempotent
requests and the retry-worthy statuses are retried, and never a write.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from mkvkit.logging import register_secret

from .config import Config, require_server
from .errors import ItemNotFound, ServerRefused

__all__ = ["Client", "Retry", "UrlOpener", "redacted"]

log = logging.getLogger(__name__)

#: Anything that behaves like ``urllib.request.urlopen``. Injected in tests.
UrlOpener = Callable[..., Any]

#: Requests that change nothing and may safely be repeated.
IDEMPOTENT = frozenset({"GET", "HEAD"})


#: Body keys whose values are credentials. Matched case-insensitively on
#: the key, anywhere in the body, and replaced before a body is logged.
SECRET_KEY = re.compile(r"token|password|passwd|secret|api_?key|credential", re.I)
REDACTED = "<redacted>"


def redacted(body: Any) -> Any:
    """A copy of a body with every credential-looking value replaced.

    The dry run logs each write's body in full, which is what makes it an
    account of what would happen; a body that carries a credential -- a
    plugin configuration with an API key in it, say -- must not put it in a
    log file on the way. The token the client itself holds is never in a
    body, and the logging filter hides it anyway once it has been resolved.
    """
    if isinstance(body, Mapping):
        return {
            key: REDACTED if isinstance(key, str) and SECRET_KEY.search(key)
            and value not in (None, "") else redacted(value)
            for key, value in body.items()
        }
    if isinstance(body, list | tuple):
        return [redacted(value) for value in body]
    return body


@dataclass(frozen=True)
class Retry:
    """How hard to try again, and when not to.

    Retrying a write is how one refresh becomes three. Only idempotent methods
    are retried here, and only on the statuses that mean "not now" rather than
    "no".
    """

    attempts: int = 3
    backoff_s: float = 0.5
    statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504})

    def delay(self, attempt: int) -> float:
        return self.backoff_s * (2.0 ** (attempt - 1))


@dataclass
class Client:
    """A thin client for one server, holding one token, passed explicitly.

    Construct it from a :class:`~jfkit.config.Config`; it never reads a file,
    an environment variable or a password manager by itself beyond the
    indirection that configuration describes.
    """

    config: Config
    dry_run: bool = True
    timeout_s: float = 30.0
    retry: Retry = field(default_factory=Retry)
    opener: UrlOpener | None = None
    environ: Mapping[str, str] | None = None
    runner: Callable[[str], str] | None = None
    user_agent: str = "jfkit"
    _token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        server = require_server(self.config)
        self.base_url = server.url.rstrip("/")
        self.user_id = server.user_id

    # ------------------------------------------------------------------ secrets
    @property
    def token(self) -> str:
        """Resolve the token once, and teach the log filter to hide it.

        Registering it with the redaction filter here rather than at the call
        site is deliberate: it means a value can only become usable by first
        becoming unloggable.
        """
        if self._token is None:
            token = self.config.server.token(environ=self.environ, runner=self.runner)
            register_secret(token)
            self._token = token
        return self._token

    # ----------------------------------------------------------------- requests
    def _url(self, route: str, params: Mapping[str, Any] | None = None) -> str:
        route = route if route.startswith("/") else "/" + route
        if not params:
            return f"{self.base_url}{route}"
        pairs = [
            (key, "true" if value is True else "false" if value is False else str(value))
            for key, value in params.items()
            if value is not None
        ]
        return f"{self.base_url}{route}?{urllib.parse.urlencode(pairs)}"

    def request(
        self,
        method: str,
        route: str,
        *,
        body: Any = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """One request, with the retry policy and the dry-run gate applied."""
        url = self._url(route, params)
        if method not in IDEMPOTENT and self.dry_run:
            log.info(
                "dry run: would %s %s%s", method, url,
                "" if body is None else " with body "
                + json.dumps(redacted(body), ensure_ascii=False, sort_keys=True),
            )
            return None

        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": f'MediaBrowser Token="{self.token}"',
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"

        last: Exception | None = None
        for attempt in range(1, self.retry.attempts + 1):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                opener = self.opener or urllib.request.urlopen
                with opener(request, timeout=self.timeout_s) as response:
                    raw = response.read()
                    if not raw:
                        return None
                    return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:400]
                if exc.code in self.retry.statuses and method in IDEMPOTENT \
                        and attempt < self.retry.attempts:
                    last = exc
                    self._wait(attempt, f"{exc.code} from {route}")
                    continue
                raise ServerRefused(exc.code, route, detail) from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if method in IDEMPOTENT and attempt < self.retry.attempts:
                    last = exc
                    self._wait(attempt, str(exc))
                    continue
                raise
        raise RuntimeError(f"unreachable: {last}")  # pragma: no cover

    def _wait(self, attempt: int, why: str) -> None:
        delay = self.retry.delay(attempt)
        log.warning("attempt %d failed (%s), retrying in %.1fs", attempt, why, delay)
        time.sleep(delay)

    def get(self, route: str, **params: Any) -> Any:
        return self.request("GET", route, params=params)

    def post(self, route: str, body: Any = None, **params: Any) -> Any:
        return self.request("POST", route, body=body, params=params)

    # -------------------------------------------------------------------- items
    def item(self, item_id: str, *, user_scoped: bool = True) -> dict[str, Any]:
        """One item's full record.

        User-scoped by default because the unscoped single-item route is not
        merely narrower -- on 12.x it refuses the request outright, which is at
        least honest, unlike its collection sibling.
        """
        route = (
            f"/Users/{self._require_user()}/Items/{item_id}" if user_scoped
            else f"/Items/{item_id}"
        )
        try:
            found = self.get(route)
        except ServerRefused as exc:
            if exc.status == 404:
                raise ItemNotFound(item_id, route) from exc
            raise
        if not isinstance(found, dict):
            raise ItemNotFound(item_id, route)
        return found

    def items(
        self,
        *,
        user_scoped: bool = True,
        page_size: int = 500,
        **params: Any,
    ) -> Iterator[dict[str, Any]]:
        """Every item matching the query, page by page.

        Paged rather than returned whole because the answer to a library-wide
        query is large enough that holding two copies of it -- the JSON and the
        list -- is a measurable amount of memory for no benefit.

        ``user_scoped`` defaults to true. The unscoped route returns a short
        answer instead of an error, which makes it the most dangerous default
        available: an audit built on it under-reports and says nothing.
        """
        route = f"/Users/{self._require_user()}/Items" if user_scoped else "/Items"
        if not user_scoped:
            log.warning(
                "querying %s without a user: this route is known to omit items on "
                "12.x, and it will not tell you which", route,
            )
        start = 0
        while True:
            page = self.get(route, startIndex=start, limit=page_size, **params)
            rows = (page or {}).get("Items") or []
            yield from rows
            total = (page or {}).get("TotalRecordCount")
            start += len(rows)
            if not rows or (total is not None and start >= int(total)):
                return

    def _require_user(self) -> str:
        if not self.user_id:
            raise ValueError(
                "server.user_id is required: item queries are user-scoped, because "
                "the unscoped route omits items instead of failing"
            )
        return self.user_id

    # ----------------------------------------------------------------- sessions
    def sessions(self) -> Sequence[dict[str, Any]]:
        found = self.get("/Sessions")
        return found if isinstance(found, list) else []

    def playing(self) -> Sequence[dict[str, Any]]:
        """Sessions that are in the middle of something."""
        return [
            session for session in self.sessions()
            if session.get("NowPlayingItem") is not None
            and not (session.get("PlayState") or {}).get("IsPaused", False)
        ]

    def wait_idle(
        self, *, timeout_s: float = 3600.0, poll_s: float = 15.0,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        """Block until nobody is mid-episode, or give up and say so.

        Every destructive pipeline should call this, and in the scripts this
        replaces not one of them did. Stopping a server, swapping a file or
        rewriting a header under somebody's playback is the kind of failure
        that is remembered long after the batch job is forgotten.
        """
        rest = sleep or time.sleep
        deadline = time.monotonic() + timeout_s
        while True:
            busy = self.playing()
            if not busy:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"{len(busy)} session(s) still playing after {timeout_s:.0f}s"
                )
            log.info("%d session(s) playing, waiting %.0fs", len(busy), poll_s)
            rest(poll_s)
