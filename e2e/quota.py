"""Write-quota pacing and 429 retry for the blackbox e2e suite.

Why this exists
---------------
The Docs API caps one user at **60 write requests per minute**
(``WriteRequestsPerMinutePerUser``), and a *failed* write counts against
the budget exactly like a successful one. The e2e suite is write-dense:
every scratch doc is a ``documents.create``, every suggestion a
``documents.batchUpdate``. Run unthrottled it sprints past 60 in well
under a minute, and from there every further call 429s -- including the
``create_doc`` inside ``make_scratch_doc``, which lands as a *fixture
error* and reads like a broken test rather than a quota wall.

The three parts, in the order they matter:

1. **Pacing** (:class:`WritePacer`) - a sliding 60 s window over write
   cost. Calls wait *before* spending rather than sprinting into the wall
   and backing off afterwards. The window is persisted (see
   :class:`WindowStore`) so a second run started seconds after the first
   inherits the first run's spend instead of starting blind.
2. **Retry** (:func:`guarded_tool_call`) - a 429 is transient and is
   retried with exponential backoff, honouring ``Retry-After`` /
   ``retryDelay`` when the API sends one. Anything else - a 400, a 404, an
   assertion - is re-raised untouched on the first attempt. Retries are
   applied at the ``ServerSession`` seam, so **fixtures get them too**.
3. **Accounting** (:class:`QuotaStats`) - what the run spent, how often it
   was rate limited, how long it waited, and whether any call gave up.
   Rendered into ``e2e/last_run.md`` so a run that hit the wall says so.

Everything here except :class:`WindowStore` and the sleeping is pure and
unit-tested in ``tests/e2e_harness/test_quota.py`` (no network, no creds).

Environment knobs
-----------------
``E2E_WRITE_BUDGET_PER_MIN``  writes/min the pacer aims for (default 50;
                              the real cap is 60 - the gap is headroom for
                              multi-request tools and concurrent sessions)
``E2E_QUOTA_MAX_ATTEMPTS``    total attempts per call, 1 disables retry
                              (default 6)
``E2E_QUOTA_PACING``          ``off`` disables pacing entirely (used to
                              measure the unpaced baseline)
``E2E_QUOTA_STATE``           ``off`` disables cross-run window sharing
``E2E_QUOTA_STATE_PATH``      explicit path for the shared window file
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Google's documented per-user Docs write ceiling. We never target this
#: number directly - see DEFAULT_BUDGET.
DOCS_WRITE_LIMIT_PER_MINUTE = 60

#: What the pacer aims for by default. The headroom absorbs (a) tools that
#: issue more than one API write per MCP call than we credit them for and
#: (b) other sessions on the same account.
DEFAULT_BUDGET = 50

#: Never adapt below this: at some point waiting is worse than failing.
MIN_BUDGET = 12

WINDOW_SECONDS = 60.0

#: Tools the server itself declares read-only via
#: ``@handle_http_errors(..., is_read_only=True)``. Anything NOT in this
#: set is paced as a write - the safe default, because new test modules
#: land with tools this file has never heard of.
#: ``tests/e2e_harness/test_quota.py`` asserts this set still matches the
#: server's own decorators, so it cannot drift silently.
READ_ONLY_TOOLS = frozenset(
    {
        "search_docs",
        "get_doc_content",
        "list_docs_in_folder",
        "inspect_doc_structure",
        "debug_docs_runtime_info",
        "debug_table_structure",
        "get_doc_as_markdown",
        "list_document_suggestions",
        "get_doc_review_view",
    }
)

#: Tools that are read-only in practice but are not decorated as such
#: (or are not decorated at all), so the is_read_only mirror above cannot
#: see them. Kept separate from READ_ONLY_TOOLS precisely so the drift
#: test stays a strict mirror of the product's own declarations.
EXTRA_READ_ONLY_TOOLS = frozenset(
    {
        "list_document_comments",  # Drive comments().list
        "start_google_auth",  # no Docs traffic at all
    }
)

#: MCP calls that cost more than one Docs write request. Values are the
#: number of write requests the tool issues; derived by reading the
#: implementations, deliberately rounded up. Under-counting here is
#: absorbed by DEFAULT_BUDGET's headroom and by adaptive shrinking.
TOOL_WRITE_COST: Mapping[str, int] = {
    # documents.create, then a batchUpdate iff content was supplied
    # (handled by _create_doc_cost).
    "create_doc": 1,
    # createTable batchUpdate + a second batchUpdate to fill the cells
    "create_table_with_data": 2,
    # header/footer create + the content insert
    "update_doc_headers_footers": 2,
}


#: Write tools that CREATE something before they can fail. Their first API
#: request may have landed when a later one was rate limited, so a retry
#: can leave the artefact of the abandoned attempt behind - an untracked
#: scratch document nothing will ever trash. Retrying is still the right
#: call (the alternative is a dead fixture), but each such retry is
#: recorded so the run report can say hygiene may need a look.
#:
#: The per-call cost weights above exist partly to make this rare: paying
#: for both of ``create_doc``'s requests up front means the pacer rarely
#: lets the second one hit the wall.
CREATING_WRITE_TOOLS = frozenset({"create_doc", "create_table_with_data"})


def _create_doc_cost(arguments: Mapping[str, Any] | None) -> int:
    return 2 if (arguments or {}).get("content") else 1


_COST_OVERRIDES: Mapping[str, Callable[[Mapping[str, Any] | None], int]] = {
    "create_doc": _create_doc_cost,
}


def is_write_tool(tool: str) -> bool:
    """True when calling ``tool`` spends Docs write quota.

    Unknown tools count as writes on purpose: over-pacing a read costs
    seconds, under-pacing a write costs the whole run.
    """
    return tool not in READ_ONLY_TOOLS and tool not in EXTRA_READ_ONLY_TOOLS


def write_cost(tool: str, arguments: Mapping[str, Any] | None = None) -> int:
    """Write requests ``tool`` is expected to issue (0 for reads)."""
    if not is_write_tool(tool):
        return 0
    override = _COST_OVERRIDES.get(tool)
    if override is not None:
        return override(arguments)
    return TOOL_WRITE_COST.get(tool, 1)


# ---------------------------------------------------------------------------
# Recognising a rate limit (and nothing else)
# ---------------------------------------------------------------------------

#: googleapiclient renders every failure as ``<HttpError NNN when ...>``;
#: the MCP surface passes that string through in the tool error text. This
#: is anchored on the status token so a 400 whose body merely contains the
#: digits 429 is not mistaken for a rate limit.
_HTTP_ERROR_429 = re.compile(r"httperror\s+429\b")

#: Substrings that only ever appear in quota/rate-limit responses.
_RATE_MARKERS: tuple[str, ...] = (
    "writerequestsperminuteperuser",
    "ratelimitexceeded",
    "userratelimitexceeded",
    "rate_limit_exceeded",
    "resource_exhausted",
    "quota exceeded",
    "too many requests",
)

#: The specific metric this suite collides with.
_WRITE_QUOTA_MARKERS: tuple[str, ...] = (
    "writerequestsperminuteperuser",
    "write requests per minute per user",
)


def is_rate_limit_error(text: str | None) -> bool:
    """True iff ``text`` is a 429 / quota rejection.

    Deliberately narrow. A 400 is a bug in the code under test and must
    fail on the first attempt, loudly; only transient rate limiting is
    worth retrying.
    """
    if not text:
        return False
    lowered = text.lower()
    if _HTTP_ERROR_429.search(lowered):
        return True
    return any(marker in lowered for marker in _RATE_MARKERS)


def is_write_quota_error(text: str | None) -> bool:
    """True iff the rate limit names the per-minute *write* metric."""
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _WRITE_QUOTA_MARKERS)


_RETRY_AFTER_TEXT = re.compile(r"retry[-_ ]?after[\"'\s:=]+(\d+(?:\.\d+)?)", re.I)
_RETRY_DELAY_TEXT = re.compile(r"retrydelay[\"'\s:]+[\"'](\d+(?:\.\d+)?)s[\"']", re.I)


def parse_retry_after(value: Any) -> float | None:
    """Seconds from a ``Retry-After`` header value (numeric form only).

    Google's JSON APIs send integer seconds here; the HTTP-date form is
    accepted by the spec but not emitted by these endpoints, so it is
    treated as absent rather than guessed at.
    """
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def retry_after_from_headers(headers: Mapping[str, Any] | None) -> float | None:
    """Case-insensitive ``Retry-After`` lookup over response headers."""
    if not headers:
        return None
    for key, value in headers.items():
        if str(key).lower() == "retry-after":
            return parse_retry_after(value)
    return None


def retry_after_from_text(text: str | None) -> float | None:
    """Best-effort ``Retry-After`` / ``retryDelay`` recovery from a message.

    The MCP surface is text-only: a tool error carries the *rendered*
    HttpError, not its headers. When the server's message happens to have
    preserved a retry hint we use it; when it has not (the common case)
    the caller falls back to exponential backoff.
    """
    if not text:
        return None
    match = _RETRY_DELAY_TEXT.search(text)
    if match:
        return float(match.group(1))
    match = _RETRY_AFTER_TEXT.search(text)
    if match:
        return float(match.group(1))
    return None


def retry_after_from_exception(exc: BaseException) -> float | None:
    """``Retry-After`` from a googleapiclient ``HttpError``, else None."""
    resp = getattr(exc, "resp", None)
    header_value = retry_after_from_headers(resp if isinstance(resp, Mapping) else None)
    if header_value is not None:
        return header_value
    if resp is not None and hasattr(resp, "get"):
        try:
            return parse_retry_after(resp.get("retry-after"))
        except Exception:  # noqa: BLE001 - header bags are duck-typed
            return None
    return None


def http_status(exc: BaseException) -> int | None:
    """HTTP status of a googleapiclient ``HttpError`` (None if not one)."""
    return getattr(getattr(exc, "resp", None), "status", None)


def backoff_delay(
    attempt: int,
    *,
    retry_after: float | None = None,
    floor: float = 0.0,
    base: float = 2.0,
    cap: float = 70.0,
    jitter: float = 0.25,
    rand: Callable[[], float] = random.random,
) -> float:
    """Seconds to wait before retry number ``attempt`` (1-based).

    ``retry_after`` (server-supplied) wins over the exponential curve;
    ``floor`` lets the caller demand at least as long as the pacing window
    needs to free capacity. The cap exceeds 60 s on purpose: the quota
    being waited on is itself a 60 s window, so a shorter ceiling would
    guarantee another collision.

    Two ordering rules, both of which used to be the other way round:

    * **The cap is applied last.** ``min(delay, cap) * (1 + jitter*rand())``
      multiplies straight through the ceiling, so attempt 10 with the jitter
      at maximum slept ``70 * 1.25 = 87.5`` s.
    * **``Retry-After`` is not jittered.** It is the server saying when to
      come back; scaling it up is us deciding it meant something else, and a
      30 s hint became a 37.5 s sleep. The ``floor`` may still hold the call
      longer - that is our own pacing window, not a reinterpretation of
      theirs - and the cap still applies, as it always did.
    """
    if retry_after is not None:
        return min(max(retry_after, floor), cap)
    exponential = base * (2 ** max(0, attempt - 1))
    delay = max(exponential, floor) * (1.0 + jitter * rand())
    return min(delay, cap)


# ---------------------------------------------------------------------------
# The sliding window
# ---------------------------------------------------------------------------


@dataclass
class WriteWindow:
    """A sliding ``window`` of write cost, capped at ``limit``.

    Pure: every method takes ``now`` from the caller, so the whole thing is
    testable with a fake clock.
    """

    limit: int = DEFAULT_BUDGET
    window: float = WINDOW_SECONDS
    entries: list[tuple[float, int]] = field(default_factory=list)

    def prune(self, now: float) -> None:
        cutoff = now - self.window
        self.entries = [e for e in self.entries if e[0] > cutoff]

    def spent(self, now: float) -> int:
        self.prune(now)
        return sum(cost for _, cost in self.entries)

    def record(self, now: float, cost: int) -> None:
        if cost > 0:
            self.entries.append((now, cost))

    def extend(self, entries: Iterable[tuple[float, int]]) -> None:
        self.entries.extend(entries)

    def delay_until_capacity(self, cost: int, now: float) -> float:
        """Seconds to wait before ``cost`` more units fit in the window.

        0.0 when there is room right now. A cost larger than the whole
        limit can never "fit"; for that case we wait for the window to
        drain completely and then allow it through, because refusing would
        make the call impossible rather than slow.
        """
        if cost <= 0:
            return 0.0
        self.prune(now)
        if not self.entries:
            return 0.0
        spent = sum(c for _, c in self.entries)
        if cost >= self.limit:
            # Needs the whole window: wait for the oldest entry to expire.
            return max(0.0, self.entries[0][0] + self.window - now)
        if spent + cost <= self.limit:
            return 0.0
        # Drop the oldest entries until the newcomer fits.
        freed = 0
        for timestamp, entry_cost in sorted(self.entries):
            freed += entry_cost
            if spent - freed + cost <= self.limit:
                return max(0.0, timestamp + self.window - now)
        return max(0.0, self.entries[-1][0] + self.window - now)


# ---------------------------------------------------------------------------
# Cross-process window sharing
# ---------------------------------------------------------------------------


def default_state_path(account: str | None = None) -> Path:
    """Where the shared window lives.

    Deliberately outside the repo and outside the worktree: the quota
    belongs to the *Google account*, not to a checkout, and several
    worktrees may be running the suite against the same account at once.
    """
    override = (os.getenv("E2E_QUOTA_STATE_PATH") or "").strip()
    if override:
        return Path(override).expanduser()
    cache_root = (os.getenv("XDG_CACHE_HOME") or "").strip()
    base = Path(cache_root).expanduser() if cache_root else Path.home() / ".cache"
    digest = hashlib.sha256((account or "default").encode("utf-8")).hexdigest()[:16]
    return base / "gdocs-review-mcp" / f"e2e-write-window-{digest}.json"


class WindowStore:
    """JSON-backed, flock-guarded write log shared by concurrent sessions.

    Timestamps are wall clock (``time.time``) because monotonic clocks are
    not comparable across processes. Every operation is best-effort: a
    corrupt or unwritable state file degrades to in-memory-only pacing
    rather than failing the suite.

    **Every access takes a lock, readers included.** ``append`` truncates
    the file and then writes it, so between those two syscalls the shared
    window is an EMPTY file - and an unlocked reader that lands there parses
    nothing and reports a spend of zero. A second session then believes it
    has the whole 60 s budget and sprints into the wall this class exists to
    keep it away from. Reads take ``LOCK_SH``, so they wait for a
    half-written file instead of observing one.
    """

    def __init__(self, path: Path, window: float = WINDOW_SECONDS) -> None:
        self.path = path
        self.window = window
        self.usable = True

    @staticmethod
    def _parse(raw: Any) -> list[tuple[float, int]]:
        entries = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            return []
        out: list[tuple[float, int]] = []
        for item in entries:
            if (
                isinstance(item, Sequence)
                and not isinstance(item, (str, bytes))
                and len(item) == 2
            ):
                try:
                    out.append((float(item[0]), int(item[1])))
                except (TypeError, ValueError):
                    continue
        return out

    def _read(self) -> list[tuple[float, int]]:
        """The whole log, under a SHARED lock (see the class docstring)."""
        import fcntl

        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                try:
                    text = handle.read()
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            raw = json.loads(text) if text.strip() else {}
        except (OSError, ValueError):
            return []
        return self._parse(raw)

    def load(self, now: float) -> list[tuple[float, int]]:
        """Entries from the shared log still inside the window."""
        if not self.usable:
            return []
        cutoff = now - self.window
        try:
            return [e for e in self._read() if e[0] > cutoff]
        except Exception:  # noqa: BLE001 - pacing must never break a run
            self.usable = False
            return []

    def _read_locked(self, handle: Any, now: float) -> list[tuple[float, int]]:
        """The in-window entries, read through an ALREADY-locked handle."""
        handle.seek(0)
        text = handle.read()
        try:
            raw = json.loads(text) if text.strip() else {}
        except ValueError:
            raw = {}
        cutoff = now - self.window
        return [e for e in self._parse(raw) if e[0] > cutoff]

    @staticmethod
    def _write_locked(handle: Any, entries: Sequence[tuple[float, int]]) -> None:
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"entries": [[t, c] for t, c in entries]}))
        handle.flush()

    def reserve(
        self, now: float, cost: int, *, limit: int, force: bool
    ) -> tuple[float, list[tuple[float, int]]]:
        """Check capacity and take it in ONE atomic step.

        Returns ``(0.0, window)`` when the spend was recorded, or
        ``(delay, window)`` naming the seconds until it would fit - having
        recorded nothing. ``(0.0, [])`` means the store is unusable and the
        caller should fall back to its in-memory window.

        This exists because :meth:`WritePacer.acquire` used to read the
        capacity through :meth:`load` and append through :meth:`append`, with
        the lock held only for the second half. Two sessions could each
        observe an empty window and each admit a full-budget write, so the
        shared budget was exceeded by exactly the number of sessions racing.
        Check and reserve now happen under one exclusive lock.

        ``force`` records regardless of capacity: the caller has run out of
        patience and is spending anyway, and the accounting must still be
        honest about it.
        """
        if not self.usable or cost <= 0:
            return 0.0, []
        try:
            import fcntl

            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    kept = self._read_locked(handle, now)
                    delay = (
                        0.0
                        if force
                        else WriteWindow(
                            limit=limit, window=self.window, entries=list(kept)
                        ).delay_until_capacity(cost, now)
                    )
                    if delay > 0:
                        return delay, kept
                    kept.append((now, cost))
                    self._write_locked(handle, kept)
                    return 0.0, kept
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:  # noqa: BLE001 - pacing must never break a run
            self.usable = False
            return 0.0, []

    def append(self, now: float, cost: int) -> list[tuple[float, int]]:
        """Record a spend unconditionally and return the pruned window.

        :meth:`reserve` with ``force=True``; kept as a name because "write
        this down" is what most callers mean and the capacity question is
        :meth:`WritePacer.acquire`'s alone.
        """
        _, kept = self.reserve(now, cost, limit=0, force=True)
        return kept


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------


@dataclass
class QuotaStats:
    """What the run spent. Rendered into the run report."""

    calls: int = 0
    write_calls: int = 0
    write_units: int = 0
    rate_limited: int = 0
    write_quota_hits: int = 0
    retries: int = 0
    paced_seconds: float = 0.0
    backoff_seconds: float = 0.0
    exhausted: list[str] = field(default_factory=list)
    budget_reductions: int = 0
    per_tool: dict[str, int] = field(default_factory=dict)
    orphan_risk_retries: list[str] = field(default_factory=list)

    def snapshot(self, budget: int) -> dict[str, Any]:
        return {
            "budget_per_minute": budget,
            "documented_limit": DOCS_WRITE_LIMIT_PER_MINUTE,
            "calls": self.calls,
            "write_calls": self.write_calls,
            "write_units": self.write_units,
            "rate_limited": self.rate_limited,
            "write_quota_hits": self.write_quota_hits,
            "retries": self.retries,
            "paced_seconds": round(self.paced_seconds, 1),
            "backoff_seconds": round(self.backoff_seconds, 1),
            "budget_reductions": self.budget_reductions,
            "exhausted": list(self.exhausted),
            "orphan_risk_retries": list(self.orphan_risk_retries),
            "top_write_tools": sorted(
                self.per_tool.items(), key=lambda kv: (-kv[1], kv[0])
            )[:10],
        }


class WriteQuotaExhausted(RuntimeError):
    """A call kept getting 429 after every retry.

    Raised rather than skipped on purpose: a test that could not run
    because the account was out of write quota is not a passing test and
    must not be reported as one.
    """


# ---------------------------------------------------------------------------
# The pacer
# ---------------------------------------------------------------------------


class WritePacer:
    """Keeps the suite under the write ceiling instead of bouncing off it."""

    def __init__(
        self,
        budget: int = DEFAULT_BUDGET,
        *,
        store: WindowStore | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        stats: QuotaStats | None = None,
        enabled: bool = True,
    ) -> None:
        self.window = WriteWindow(limit=budget)
        self.store = store
        self.clock = clock
        self.sleeper = sleeper
        self.stats = stats if stats is not None else QuotaStats()
        self.enabled = enabled

    @property
    def budget(self) -> int:
        return self.window.limit

    def _sync(self, now: float) -> None:
        """Fold the shared log into the local window."""
        if self.store is None:
            self.window.prune(now)
            return
        shared = self.store.load(now)
        if shared:
            self.window.entries = shared
        else:
            self.window.prune(now)

    def seconds_until_capacity(self, cost: int) -> float:
        """How long ``cost`` would have to wait, WITHOUT reserving it.

        Advisory only - it is the retry backoff's ``floor``. Anything that
        intends to spend must go through :meth:`acquire`, whose check and
        reservation are one atomic step; asking this and then spending is
        precisely the race :meth:`WindowStore.reserve` exists to close.
        """
        now = self.clock()
        self._sync(now)
        return self.window.delay_until_capacity(cost, now)

    def _reserve(self, now: float, cost: int, *, force: bool) -> float:
        """Take ``cost`` from the window if it fits; report the wait if not.

        Returns 0.0 once the spend is recorded. With a shared store the whole
        check-and-reserve happens under one exclusive flock
        (:meth:`WindowStore.reserve`); without one there is no other party to
        race and the local window answers directly.
        """
        if self.store is None:
            self.window.prune(now)
            delay = 0.0 if force else self.window.delay_until_capacity(cost, now)
            if delay <= 0:
                self.window.record(now, cost)
            return delay
        delay, shared = self.store.reserve(
            now, cost, limit=self.window.limit, force=force
        )
        if shared:
            self.window.entries = list(shared)
        elif delay <= 0:
            # The store went unusable: keep pacing off the local window.
            self.window.record(now, cost)
        return delay

    def acquire(self, cost: int) -> float:
        """Wait until ``cost`` units fit, then record the spend.

        Returns the seconds slept. Records unconditionally (even with
        pacing disabled, and even after running out of patience) so the
        accounting stays honest.
        """
        slept = 0.0
        if cost <= 0:
            return 0.0
        if self.enabled:
            # Loop rather than sleep once: another session may take the
            # capacity we were waiting for. Each pass re-checks AND reserves
            # in one step, so the capacity we were told about is the capacity
            # we get.
            for _ in range(24):
                delay = self._reserve(self.clock(), cost, force=False)
                if delay <= 0:
                    self.stats.paced_seconds += slept
                    return slept
                delay = min(delay, WINDOW_SECONDS)
                self.sleeper(delay)
                slept += delay
        # Out of patience, or pacing is off: spend anyway, and record it.
        self._reserve(self.clock(), cost, force=True)
        self.stats.paced_seconds += slept
        return slept

    def note_rate_limit(self) -> None:
        """Shrink the target budget after an observed 429.

        Our cost model is an estimate and other sessions share the
        account, so the only trustworthy signal that the budget is too
        high is the API saying so.
        """
        new_limit = max(MIN_BUDGET, int(self.window.limit * 0.8))
        if new_limit < self.window.limit:
            self.window.limit = new_limit
            self.stats.budget_reductions += 1


# ---------------------------------------------------------------------------
# The guarded call
# ---------------------------------------------------------------------------


def _result_text(result: Any) -> str:
    """Text blocks of a fastmcp CallToolResult (kept local to dodge a cycle)."""
    content = getattr(result, "content", None) or []
    parts = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def rate_limited_result_text(result: Any, cost: int) -> str | None:
    """Rate-limit text carried by ``result``, in either shape it can arrive.

    Two shapes, both seen against prod:

    * ``is_error`` set - the usual path, where ``handle_http_errors`` let
      the ``HttpError`` become a tool error.
    * ``is_error`` NOT set, with the 429 rendered into the *body* as prose.
      ``update_doc_headers_footers`` does exactly this: it catches the
      failure from its manager and returns ``"Error: Failed to write
      <segment> content: <HttpError 429 ...>"`` as a perfectly successful
      MCP result. Empirically caught on 2026-08-02; without this branch the
      guard sees a success, does not retry, and the test fails on an
      assertion about the response body - a quota failure wearing the
      costume of a product bug.

    The in-band branch is restricted to write calls (``cost > 0``) so that
    a read echoing a document's contents can never be mistaken for one.
    """
    text = _result_text(result)
    if getattr(result, "is_error", False):
        return text if is_rate_limit_error(text) else None
    if cost > 0 and is_rate_limit_error(text):
        return text
    return None


@dataclass
class QuotaGuard:
    """Pacing + 429 retry wrapped around one MCP tool invocation."""

    pacer: WritePacer
    max_attempts: int = 6
    sleeper: Callable[[float], None] = time.sleep
    rand: Callable[[], float] = random.random

    @property
    def stats(self) -> QuotaStats:
        return self.pacer.stats

    def _observe_rate_limit(self, text: str) -> None:
        self.stats.rate_limited += 1
        if is_write_quota_error(text):
            self.stats.write_quota_hits += 1
        self.pacer.note_rate_limit()

    def _wait(self, attempt: int, cost: int, text: str | None, tool: str) -> None:
        if tool in CREATING_WRITE_TOOLS:
            self.stats.orphan_risk_retries.append(tool)
        floor = self.pacer.seconds_until_capacity(max(cost, 1))
        delay = backoff_delay(
            attempt,
            retry_after=retry_after_from_text(text),
            floor=floor,
            rand=self.rand,
        )
        self.stats.retries += 1
        self.stats.backoff_seconds += delay
        self.sleeper(delay)

    def call(
        self,
        tool: str,
        arguments: Mapping[str, Any] | None,
        invoke: Callable[[], Any],
    ) -> Any:
        """Run ``invoke`` under pacing, retrying only on 429.

        ``invoke`` is a zero-argument thunk performing the real MCP call;
        it is re-run from scratch on each attempt, which is safe because
        every retried call is one the API rejected without applying.
        """
        cost = write_cost(tool, arguments)
        self.stats.calls += 1
        if cost:
            self.stats.write_calls += 1
            self.stats.per_tool[tool] = self.stats.per_tool.get(tool, 0) + 1
        last_text: str | None = None

        for attempt in range(1, self.max_attempts + 1):
            self.pacer.acquire(cost)
            self.stats.write_units += cost
            try:
                result = invoke()
            except Exception as exc:  # noqa: BLE001 - re-raised unless 429
                text = str(exc)
                if not is_rate_limit_error(text) or attempt == self.max_attempts:
                    if is_rate_limit_error(text):
                        self._observe_rate_limit(text)
                        self.stats.exhausted.append(
                            f"{tool} ({self.max_attempts} 429s)"
                        )
                        raise WriteQuotaExhausted(
                            f"{tool}: still rate limited after "
                            f"{self.max_attempts} attempts. This run did NOT "
                            f"exercise what it claims to.\n  last error: {text[:400]}"
                        ) from exc
                    raise
                self._observe_rate_limit(text)
                last_text = text
                self._wait(attempt, cost, text, tool)
                continue

            text = rate_limited_result_text(result, cost)
            if text is None:
                return result
            self._observe_rate_limit(text)
            last_text = text
            if attempt == self.max_attempts:
                self.stats.exhausted.append(f"{tool} ({self.max_attempts} 429s)")
                raise WriteQuotaExhausted(
                    f"{tool}: still rate limited after {self.max_attempts} "
                    f"attempts. This run did NOT exercise what it claims to.\n"
                    f"  last error: {text[:400]}"
                )
            self._wait(attempt, cost, text, tool)

        raise WriteQuotaExhausted(  # pragma: no cover - loop always returns/raises
            f"{tool}: exhausted retries; last error: {(last_text or '')[:400]}"
        )


def retry_google_call(
    call: Callable[[], Any],
    *,
    label: str,
    stats: QuotaStats | None = None,
    max_attempts: int = 5,
    sleeper: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
) -> Any:
    """Retry a *direct* googleapiclient call on 429 only.

    Used by the harness-side Drive client (scratch-doc teardown, audits),
    which does not go through the MCP surface and therefore still has real
    response headers - so ``Retry-After`` is honoured properly here.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - re-raised unless 429
            status = http_status(exc)
            text = str(exc)
            transient = status == 429 or (status is None and is_rate_limit_error(text))
            if not transient or attempt == max_attempts:
                raise
            if stats is not None:
                stats.rate_limited += 1
                stats.retries += 1
            delay = backoff_delay(
                attempt,
                retry_after=retry_after_from_exception(exc)
                or retry_after_from_text(text),
                rand=rand,
            )
            if stats is not None:
                stats.backoff_seconds += delay
            sleeper(delay)
    raise RuntimeError(f"unreachable retry exit for {label}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Process-wide singleton (the suite has exactly one quota to spend)
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_off(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"0", "off", "false", "no"}


def build_guard(account: str | None = None) -> QuotaGuard:
    """Build the guard from the environment (called once per process)."""
    budget = max(1, _env_int("E2E_WRITE_BUDGET_PER_MIN", DEFAULT_BUDGET))
    attempts = max(1, _env_int("E2E_QUOTA_MAX_ATTEMPTS", 6))
    store = None
    if not _env_off("E2E_QUOTA_STATE"):
        store = WindowStore(default_state_path(account))
    pacer = WritePacer(budget, store=store, enabled=not _env_off("E2E_QUOTA_PACING"))
    return QuotaGuard(pacer=pacer, max_attempts=attempts)


GUARD = build_guard()
#: Convenience alias - the singleton's stats object, fed to the run report.
STATS = GUARD.stats


def bind_account(email: str) -> None:
    """Re-key the shared window on the account actually being used.

    The guard is built at import time, before ``ga_auth`` has resolved the
    token, so it starts on the ``default`` key. Once the identity is known
    the store is re-pointed so two checkouts driving the *same* Google
    account share one window - and two accounts do not.
    """
    if GUARD.pacer.store is None or not email:
        return
    GUARD.pacer.store = WindowStore(default_state_path(email))


def snapshot() -> dict[str, Any]:
    return GUARD.stats.snapshot(GUARD.pacer.budget)
