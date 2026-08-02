"""Unit tests for the e2e write-quota pacer/retry (``e2e/quota.py``).

No network, no credentials, no sleeping: the clock and the sleep function
are injected everywhere, so the whole 60-second-window story runs in
microseconds.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from e2e import quota
from e2e.run_report import RunReport

REPO_ROOT = Path(__file__).resolve().parents[2]

# The shape the Docs API actually returns when the suite outruns the
# write ceiling, as rendered through googleapiclient + the server's
# handle_http_errors wrapper.
RATE_LIMIT_TEXT = (
    "API error in create_doc: <HttpError 429 when requesting "
    "https://docs.googleapis.com/v1/documents?alt=json returned "
    "\"Quota exceeded for quota metric 'Write requests' and limit "
    "'Write requests per minute per user' of service 'docs.googleapis.com' "
    "for consumer 'project_number:111222333'.\". Details: \"[{'@type': "
    "'type.googleapis.com/google.rpc.ErrorInfo', 'reason': "
    "'RATE_LIMIT_EXCEEDED', 'domain': 'googleapis.com', 'metadata': "
    "{'quota_limit': 'WriteRequestsPerMinutePerUser', "
    "'quota_limit_value': '60'}}]\""
)

# A real bug, not a transient: must fail on the first attempt.
BAD_REQUEST_TEXT = (
    "API error in suggest_doc_edit: <HttpError 400 when requesting "
    "https://docs.googleapis.com/v1/documents/abc123:batchUpdate?alt=json "
    'returned "Invalid requests[0].replaceAllText: The index 429 is out of '
    'bounds.">'
)


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeResult:
    """Minimal stand-in for a fastmcp CallToolResult."""

    def __init__(self, text: str, is_error: bool = False) -> None:
        self.content = [type("Block", (), {"text": text})()]
        self.is_error = is_error


# ---------------------------------------------------------------------------
# Read/write classification
# ---------------------------------------------------------------------------


def test_declared_read_tools_cost_nothing():
    for tool in quota.READ_ONLY_TOOLS | quota.EXTRA_READ_ONLY_TOOLS:
        assert not quota.is_write_tool(tool)
        assert quota.write_cost(tool) == 0


def test_write_tools_cost_at_least_one():
    for tool in ("suggest_doc_edit", "manage_document_suggestion", "modify_doc_text"):
        assert quota.is_write_tool(tool)
        assert quota.write_cost(tool) == 1


def test_unknown_tools_are_paced_as_writes():
    """New test modules land with tools this file has never seen; the safe
    default is to treat them as writes."""
    assert quota.is_write_tool("some_tool_invented_tonight")
    assert quota.write_cost("some_tool_invented_tonight") == 1


def test_create_doc_with_content_costs_two_writes():
    assert quota.write_cost("create_doc", {"title": "t"}) == 1
    assert quota.write_cost("create_doc", {"title": "t", "content": "hi"}) == 2
    assert quota.write_cost("create_doc", None) == 1


def test_read_only_set_mirrors_the_servers_own_declaration():
    """READ_ONLY_TOOLS must stay a mirror of ``is_read_only=True``.

    If a future tool is decorated read-only and this set is not updated it
    only gets paced unnecessarily; the dangerous direction is a tool
    listed here that the server considers a write, so both directions are
    asserted.
    """
    pattern = re.compile(
        r"@handle_http_errors\(\s*[\"']([a-z_]+)[\"']([^)]*)\)", re.DOTALL
    )
    declared_reads: set[str] = set()
    declared_writes: set[str] = set()
    for relative in (
        "gdocs/docs_tools.py",
        "gdocs_preview/curated_tools.py",
        "gdocs_preview/write_tools.py",
    ):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        for name, rest in pattern.findall(source):
            if "is_read_only=True" in rest:
                declared_reads.add(name)
            else:
                declared_writes.add(name)

    assert declared_reads, "decorator scan found nothing - did the API change?"
    assert quota.READ_ONLY_TOOLS == declared_reads, (
        "e2e/quota.py READ_ONLY_TOOLS has drifted from the server's "
        f"is_read_only declarations; server says: {sorted(declared_reads)}"
    )
    overlap = declared_writes & (quota.READ_ONLY_TOOLS | quota.EXTRA_READ_ONLY_TOOLS)
    assert not overlap, f"paced as reads but declared writes by the server: {overlap}"


# ---------------------------------------------------------------------------
# Recognising a rate limit and nothing else
# ---------------------------------------------------------------------------


def test_rate_limit_text_is_recognised():
    assert quota.is_rate_limit_error(RATE_LIMIT_TEXT)
    assert quota.is_write_quota_error(RATE_LIMIT_TEXT)


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        BAD_REQUEST_TEXT,
        "API error in modify_doc_text: <HttpError 404 when requesting ...>",
        "assertion failed: expected 429 suggestions, got 3",
    ],
)
def test_non_rate_limit_text_is_not_retryable(text):
    assert not quota.is_rate_limit_error(text)


def test_bare_429_digits_do_not_look_like_a_rate_limit():
    """A 400 whose payload mentions the number 429 is still a bug."""
    assert not quota.is_rate_limit_error(BAD_REQUEST_TEXT)
    assert not quota.is_write_quota_error(BAD_REQUEST_TEXT)


def test_other_rate_limit_shapes_are_recognised():
    assert quota.is_rate_limit_error("Error 403: userRateLimitExceeded")
    assert quota.is_rate_limit_error("RESOURCE_EXHAUSTED")
    assert quota.is_rate_limit_error("429 Too Many Requests")
    # ... but they are not necessarily the *write* metric
    assert not quota.is_write_quota_error("Error 403: userRateLimitExceeded")


# ---------------------------------------------------------------------------
# Retry-After
# ---------------------------------------------------------------------------


def test_retry_after_from_headers_is_case_insensitive():
    assert quota.retry_after_from_headers({"Retry-After": "30"}) == 30.0
    assert quota.retry_after_from_headers({"retry-after": 12}) == 12.0
    assert quota.retry_after_from_headers({"other": "1"}) is None
    assert quota.retry_after_from_headers(None) is None


def test_retry_after_rejects_http_date_form():
    """The spec allows a date; these endpoints do not send one, so we
    treat it as absent rather than guessing."""
    assert (
        quota.retry_after_from_headers({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        is None
    )


def test_retry_after_recovered_from_message_text():
    assert quota.retry_after_from_text("'retryDelay': '17s'") == 17.0
    assert quota.retry_after_from_text("Retry-After: 45") == 45.0
    assert quota.retry_after_from_text(RATE_LIMIT_TEXT) is None


def test_retry_after_from_exception_reads_response_headers():
    class Resp(dict):
        status = 429

    error = type("HttpError", (Exception,), {})()
    error.resp = Resp({"retry-after": "9"})
    assert quota.retry_after_from_exception(error) == 9.0
    assert quota.http_status(error) == 429


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------


def test_backoff_grows_and_is_capped():
    delays = [quota.backoff_delay(n, jitter=0.0, rand=lambda: 0.0) for n in range(1, 8)]
    assert delays[:4] == [2.0, 4.0, 8.0, 16.0]
    assert all(d <= 70.0 for d in delays)
    # The ceiling must exceed the 60s quota window, or every retry lands
    # inside the same window it is waiting out.
    assert max(delays) > 60.0


def test_server_supplied_retry_after_wins_over_the_curve():
    assert (
        quota.backoff_delay(1, retry_after=25.0, jitter=0.0, rand=lambda: 0.0) == 25.0
    )


def test_floor_forces_waiting_out_the_pacing_window():
    delay = quota.backoff_delay(1, floor=40.0, jitter=0.0, rand=lambda: 0.0)
    assert delay == 40.0


def test_jitter_is_additive_and_bounded():
    low = quota.backoff_delay(1, jitter=0.25, rand=lambda: 0.0)
    high = quota.backoff_delay(1, jitter=0.25, rand=lambda: 1.0)
    assert low == 2.0
    assert high == 2.5


# ---------------------------------------------------------------------------
# The sliding window
# ---------------------------------------------------------------------------


def test_window_admits_until_the_budget_is_gone():
    window = quota.WriteWindow(limit=3)
    assert window.delay_until_capacity(1, 100.0) == 0.0
    window.record(100.0, 3)
    assert window.spent(100.0) == 3
    # Full: must wait for the 100.0 entry to fall out at 160.0.
    assert window.delay_until_capacity(1, 100.0) == pytest.approx(60.0)
    assert window.delay_until_capacity(1, 130.0) == pytest.approx(30.0)
    assert window.delay_until_capacity(1, 161.0) == 0.0


def test_window_only_waits_for_as_many_old_entries_as_it_needs():
    window = quota.WriteWindow(limit=3)
    window.record(100.0, 1)
    window.record(120.0, 1)
    window.record(140.0, 1)
    # Freeing the oldest (expires at 160.0) is enough for one more unit.
    assert window.delay_until_capacity(1, 150.0) == pytest.approx(10.0)
    # Two units need the second entry gone too (expires at 180.0).
    assert window.delay_until_capacity(2, 150.0) == pytest.approx(30.0)


def test_window_prunes_expired_entries():
    window = quota.WriteWindow(limit=5)
    window.record(100.0, 2)
    window.record(170.0, 1)
    assert window.spent(171.0) == 1


def test_cost_larger_than_the_budget_waits_for_a_drain_rather_than_deadlocking():
    window = quota.WriteWindow(limit=2)
    window.record(100.0, 1)
    assert window.delay_until_capacity(5, 100.0) == pytest.approx(60.0)
    assert window.delay_until_capacity(5, 161.0) == 0.0


def test_reads_never_wait():
    window = quota.WriteWindow(limit=1)
    window.record(100.0, 1)
    assert window.delay_until_capacity(0, 100.0) == 0.0


# ---------------------------------------------------------------------------
# The pacer
# ---------------------------------------------------------------------------


def test_pacer_sleeps_only_once_the_budget_is_spent():
    clock = FakeClock()
    pacer = quota.WritePacer(budget=3, clock=clock, sleeper=clock.sleep)
    for _ in range(3):
        assert pacer.acquire(1) == 0.0
    slept = pacer.acquire(1)
    assert slept == pytest.approx(60.0)
    assert pacer.stats.paced_seconds == pytest.approx(60.0)


def test_pacer_records_reads_as_free():
    clock = FakeClock()
    pacer = quota.WritePacer(budget=1, clock=clock, sleeper=clock.sleep)
    pacer.acquire(1)
    assert pacer.acquire(0) == 0.0
    assert clock.now == 1_000.0


def test_pacer_shrinks_its_budget_when_the_api_says_no():
    pacer = quota.WritePacer(budget=50)
    pacer.note_rate_limit()
    assert pacer.budget == 40
    assert pacer.stats.budget_reductions == 1


def test_pacer_budget_never_shrinks_below_the_floor():
    pacer = quota.WritePacer(budget=50)
    for _ in range(40):
        pacer.note_rate_limit()
    assert pacer.budget == quota.MIN_BUDGET


def test_disabled_pacer_still_counts():
    clock = FakeClock()
    pacer = quota.WritePacer(budget=1, clock=clock, sleeper=clock.sleep, enabled=False)
    pacer.acquire(1)
    assert pacer.acquire(1) == 0.0
    assert clock.now == 1_000.0
    assert pacer.window.spent(clock.now) == 2


# ---------------------------------------------------------------------------
# Cross-process window sharing
# ---------------------------------------------------------------------------


def test_window_store_round_trips(tmp_path):
    store = quota.WindowStore(tmp_path / "w.json")
    store.append(1_000.0, 2)
    store.append(1_001.0, 1)
    assert store.load(1_002.0) == [(1_000.0, 2), (1_001.0, 1)]


def test_window_store_drops_entries_older_than_the_window(tmp_path):
    store = quota.WindowStore(tmp_path / "w.json")
    store.append(1_000.0, 2)
    assert store.load(1_100.0) == []


def test_a_second_process_inherits_the_first_ones_spend(tmp_path):
    """The back-to-back-run case: run 2 must not start with a blind budget."""
    path = tmp_path / "shared.json"
    clock = FakeClock()
    first = quota.WritePacer(
        budget=3, store=quota.WindowStore(path), clock=clock, sleeper=clock.sleep
    )
    for _ in range(3):
        first.acquire(1)

    # A brand-new pacer, as a fresh pytest process would build.
    second = quota.WritePacer(
        budget=3, store=quota.WindowStore(path), clock=clock, sleeper=clock.sleep
    )
    assert second.seconds_until_capacity(1) == pytest.approx(60.0)
    assert second.acquire(1) == pytest.approx(60.0)


def test_corrupt_state_file_degrades_to_memory_only(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json at all", encoding="utf-8")
    store = quota.WindowStore(path)
    assert store.load(1_000.0) == []
    # ...and a subsequent append repairs rather than raises.
    assert store.append(1_000.0, 1) == [(1_000.0, 1)]
    assert json.loads(path.read_text(encoding="utf-8"))["entries"] == [[1_000.0, 1]]


def test_state_path_is_per_account_and_outside_the_repo(monkeypatch, tmp_path):
    monkeypatch.delenv("E2E_QUOTA_STATE_PATH", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    a = quota.default_state_path("a@example.com")
    b = quota.default_state_path("b@example.com")
    assert a != b
    assert REPO_ROOT not in a.parents
    monkeypatch.setenv("E2E_QUOTA_STATE_PATH", str(tmp_path / "explicit.json"))
    assert quota.default_state_path("a@example.com") == tmp_path / "explicit.json"


# ---------------------------------------------------------------------------
# The guard: retry only on 429, and account for it
# ---------------------------------------------------------------------------


def _guard(
    clock: FakeClock, *, attempts: int = 4, budget: int = 100
) -> quota.QuotaGuard:
    pacer = quota.WritePacer(budget=budget, clock=clock, sleeper=clock.sleep)
    return quota.QuotaGuard(
        pacer=pacer, max_attempts=attempts, sleeper=clock.sleep, rand=lambda: 0.0
    )


def test_guard_returns_a_good_result_without_retrying():
    clock = FakeClock()
    guard = _guard(clock)
    calls = []

    def invoke():
        calls.append(1)
        return FakeResult("ok")

    assert guard.call("suggest_doc_edit", {}, invoke) is not None
    assert len(calls) == 1
    assert guard.stats.rate_limited == 0
    assert guard.stats.write_calls == 1
    assert guard.stats.write_units == 1


def test_guard_retries_a_rate_limited_error_result_then_succeeds():
    clock = FakeClock()
    guard = _guard(clock)
    attempts = []

    def invoke():
        attempts.append(1)
        if len(attempts) < 3:
            return FakeResult(RATE_LIMIT_TEXT, is_error=True)
        return FakeResult("ok")

    guard.call("suggest_doc_edit", {}, invoke)
    assert len(attempts) == 3
    assert guard.stats.rate_limited == 2
    assert guard.stats.write_quota_hits == 2
    assert guard.stats.retries == 2
    assert guard.stats.backoff_seconds > 0
    # A failed write costs quota too, so all three attempts are charged.
    assert guard.stats.write_units == 3


def test_guard_retries_a_raised_rate_limit():
    clock = FakeClock()
    guard = _guard(clock)
    attempts = []

    def invoke():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError(RATE_LIMIT_TEXT)
        return FakeResult("ok")

    guard.call("create_doc", {"title": "t"}, invoke)
    assert len(attempts) == 2


def test_guard_does_not_retry_a_bad_request():
    """A 400 is a bug in the code under test and must fail immediately."""
    clock = FakeClock()
    guard = _guard(clock)
    attempts = []

    def invoke():
        attempts.append(1)
        raise RuntimeError(BAD_REQUEST_TEXT)

    with pytest.raises(RuntimeError) as excinfo:
        guard.call("suggest_doc_edit", {}, invoke)
    assert "HttpError 400" in str(excinfo.value)
    assert len(attempts) == 1
    assert guard.stats.rate_limited == 0


#: The exact shape update_doc_headers_footers returns when its internal
#: batchUpdate is rate limited: a SUCCESSFUL MCP result whose body is the
#: 429. Caught against prod on 2026-08-02.
IN_BAND_RATE_LIMIT_TEXT = (
    "Error: Failed to write kix.ca8fpucwjkry segment content: <HttpError 429 "
    "when requesting https://docs.googleapis.com/v1/documents/abc:batchUpdate"
    "?alt=json returned \"Quota exceeded for quota metric 'Quota group for "
    "write operations' and limit 'Quota group for write operations per minute "
    "per user' of service 'docs.googleapis.com'.\". Details: \"[{'reason': "
    "'RATE_LIMIT_EXCEEDED', 'metadata': {'quota_limit': "
    "'WriteRequestsPerMinutePerUser'}}]\". Runtime: docs-hf-canary-20260328b"
)


def test_a_write_tool_that_reports_429_in_its_body_is_still_retried():
    """Some tools swallow the HttpError and return it as prose.

    ``is_error`` is False, so without the in-band branch the guard would
    hand the test a quota failure to assert against and the run would look
    like a product bug.
    """
    clock = FakeClock()
    guard = _guard(clock)
    attempts = []

    def invoke():
        attempts.append(1)
        if len(attempts) == 1:
            return FakeResult(IN_BAND_RATE_LIMIT_TEXT, is_error=False)
        return FakeResult("Header updated. Runtime: ...")

    result = guard.call("update_doc_headers_footers", {}, invoke)
    assert "Header updated" in result.content[0].text
    assert len(attempts) == 2
    assert guard.stats.rate_limited == 1
    assert guard.stats.write_quota_hits == 1


def test_a_read_echoing_quota_prose_is_never_retried():
    """Only write calls get the in-band branch: a read returning a document
    that happens to quote a 429 is just data."""
    clock = FakeClock()
    guard = _guard(clock)
    attempts = []

    def invoke():
        attempts.append(1)
        return FakeResult(IN_BAND_RATE_LIMIT_TEXT, is_error=False)

    guard.call("get_doc_review_view", {}, invoke)
    assert len(attempts) == 1
    assert guard.stats.rate_limited == 0


def test_retrying_a_creating_tool_is_flagged_as_a_hygiene_risk():
    """A retried create may leave the first attempt's document behind."""
    clock = FakeClock()
    guard = _guard(clock)
    attempts = []

    def invoke():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError(RATE_LIMIT_TEXT)
        return FakeResult("Created Google Doc 'x' (ID: abc)")

    guard.call("create_doc", {"title": "x", "content": "y"}, invoke)
    assert guard.stats.orphan_risk_retries == ["create_doc"]
    snapshot = guard.stats.snapshot(50)
    assert snapshot["orphan_risk_retries"] == ["create_doc"]


def test_retrying_a_non_creating_tool_raises_no_hygiene_flag():
    clock = FakeClock()
    guard = _guard(clock)
    attempts = []

    def invoke():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError(RATE_LIMIT_TEXT)
        return FakeResult("ok")

    guard.call("suggest_doc_edit", {}, invoke)
    assert guard.stats.orphan_risk_retries == []


def test_report_flags_the_orphan_risk():
    report = RunReport()
    report.observe("e2e/t.py::a", {"e2e_ga"}, "call", "passed")
    stats = quota.QuotaStats(rate_limited=1, retries=1)
    stats.orphan_risk_retries.append("create_doc")
    report.set_quota(stats.snapshot(50))
    rendered = report.render_markdown()
    assert "hygiene: 1 retry/retries of a creating tool" in rendered
    assert "(abandoned retry)" in rendered


def test_guard_passes_through_a_non_rate_limited_error_result():
    """Sad-path tests assert on error results; those must arrive intact."""
    clock = FakeClock()
    guard = _guard(clock)
    result = guard.call(
        "suggest_doc_edit", {}, lambda: FakeResult(BAD_REQUEST_TEXT, is_error=True)
    )
    assert result.is_error


def test_guard_gives_up_loudly_rather_than_skipping():
    """Quota exhaustion must never be reported as a pass or a soft skip."""
    clock = FakeClock()
    guard = _guard(clock, attempts=3)

    def invoke():
        return FakeResult(RATE_LIMIT_TEXT, is_error=True)

    with pytest.raises(quota.WriteQuotaExhausted) as excinfo:
        guard.call("suggest_doc_edit", {}, invoke)
    assert "did NOT exercise what it claims to" in str(excinfo.value)
    assert guard.stats.exhausted == ["suggest_doc_edit (3 429s)"]


def test_guard_does_not_pace_reads():
    clock = FakeClock()
    guard = _guard(clock, budget=1)
    for _ in range(10):
        guard.call("get_doc_review_view", {}, lambda: FakeResult("{}"))
    assert clock.now == 1_000.0
    assert guard.stats.write_calls == 0
    assert guard.stats.calls == 10


# ---------------------------------------------------------------------------
# Direct googleapiclient retry (harness-side Drive teardown)
# ---------------------------------------------------------------------------


class _FakeHttpError(Exception):
    def __init__(self, status: int, message: str = "", headers=None) -> None:
        super().__init__(message or f"<HttpError {status} when requesting ...>")
        self.resp = type("Resp", (), {"status": status})()
        if headers is not None:
            self.resp = dict(headers)
            self.resp.status = status  # type: ignore[attr-defined]


def test_drive_retry_recovers_from_a_429():
    clock = FakeClock()
    attempts = []

    def call():
        attempts.append(1)
        if len(attempts) == 1:
            raise _FakeHttpError(429, RATE_LIMIT_TEXT)
        return "trashed"

    stats = quota.QuotaStats()
    result = quota.retry_google_call(
        call, label="trash", stats=stats, sleeper=clock.sleep, rand=lambda: 0.0
    )
    assert result == "trashed"
    assert stats.rate_limited == 1
    assert stats.backoff_seconds > 0


def test_drive_retry_reraises_a_404_immediately():
    attempts = []

    def call():
        attempts.append(1)
        raise _FakeHttpError(404)

    with pytest.raises(_FakeHttpError):
        quota.retry_google_call(call, label="trash", sleeper=lambda _: None)
    assert len(attempts) == 1


def test_drive_retry_gives_up_and_reraises_the_real_error():
    def call():
        raise _FakeHttpError(429, RATE_LIMIT_TEXT)

    with pytest.raises(_FakeHttpError):
        quota.retry_google_call(
            call,
            label="trash",
            max_attempts=2,
            sleeper=lambda _: None,
            rand=lambda: 0.0,
        )


# ---------------------------------------------------------------------------
# Reporting: a run that hit the wall must say so
# ---------------------------------------------------------------------------


def test_report_states_a_clean_quota_run():
    report = RunReport()
    report.observe("e2e/test_x.py::a", {"e2e_ga"}, "call", "passed")
    report.set_quota(
        quota.QuotaStats(calls=10, write_calls=6, write_units=8).snapshot(50)
    )
    rendered = report.render_markdown()
    assert "## Write quota" in rendered
    assert "clean - never rate limited" in rendered.replace("—", "-")
    assert "8 estimated write requests" in rendered


def test_report_shouts_when_quota_was_exhausted():
    report = RunReport()
    report.observe("e2e/test_x.py::a", {"e2e_preview"}, "setup", "failed")
    stats = quota.QuotaStats(rate_limited=12, retries=11)
    stats.exhausted.append("create_doc (6 429s)")
    report.set_quota(stats.snapshot(30))
    report.note("QUOTA: 1 call(s) never got through the write-quota wall.")
    rendered = report.render_markdown()
    assert "WALL HIT" in rendered
    assert "INCOMPLETE RUN" in rendered
    assert "create_doc (6 429s)" in rendered


def test_report_reports_absorbed_rate_limits_without_crying_wolf():
    report = RunReport()
    report.observe("e2e/test_x.py::a", {"e2e_ga"}, "call", "passed")
    report.set_quota(quota.QuotaStats(rate_limited=3, retries=3).snapshot(40))
    rendered = report.render_markdown()
    assert "absorbed" in rendered
    assert "WALL HIT" not in rendered
    assert "INCOMPLETE RUN" not in rendered


def test_report_completeness_counts_every_collected_test():
    report = RunReport()
    report.observe("e2e/t.py::a", {"e2e_ga"}, "call", "passed")
    report.observe("e2e/t.py::b", {"e2e_preview"}, "setup", "failed")
    report.observe("e2e/t.py::c", {"e2e_preview"}, "setup", "skipped", "no enrollment")
    rendered = report.render_markdown()
    assert "collected e2e tests: **3**" in rendered
    assert "passed 1, failed 1, skipped 1" in rendered
    assert "not a clean run" in rendered
