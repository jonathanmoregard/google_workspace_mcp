"""Unit tests for e2e.run_report - the artifact forwarded to the requester."""

from e2e.run_report import RunReport, resolve_report_path


class TestObserve:
    def test_passed_requires_call_phase(self):
        report = RunReport()
        report.observe("n1", {"e2e_ga"}, "setup", "passed")
        assert report.outcomes["n1"]["outcome"] is None
        report.observe("n1", {"e2e_ga"}, "call", "passed")
        assert report.outcomes["n1"]["outcome"] == "passed"

    def test_failure_wins_over_late_passed_teardown(self):
        report = RunReport()
        report.observe("n1", {"e2e_ga"}, "call", "failed")
        report.observe("n1", {"e2e_ga"}, "teardown", "passed")
        assert report.outcomes["n1"]["outcome"] == "failed"

    def test_skip_captures_reason(self):
        report = RunReport()
        report.observe("n1", {"e2e_preview"}, "setup", "skipped", "no enrollment")
        entry = report.outcomes["n1"]
        assert entry["outcome"] == "skipped"
        assert entry["skip_reason"] == "no enrollment"

    def test_marker_counts_bucketed(self):
        report = RunReport()
        report.observe("a", {"e2e_ga"}, "call", "passed")
        report.observe("b", {"e2e_ga"}, "call", "failed")
        report.observe("c", {"e2e_preview"}, "setup", "skipped", "x")
        report.observe("d", set(), "call", "passed")
        counts = report.marker_counts()
        assert counts["e2e_ga"] == {
            "passed": 1,
            "failed": 1,
            "skipped": 0,
            "unknown": 0,
        }
        assert counts["e2e_preview"]["skipped"] == 1
        assert counts["unmarked"]["passed"] == 1


class TestRenderMarkdown:
    def test_full_report_sections(self, tmp_path):
        report = RunReport()
        report.set_identity("user@example.com", "/creds", "oauth2/v2 userinfo")
        report.observe("a", {"e2e_ga"}, "call", "passed")
        report.set_preview_classification(
            {
                "availability": "unavailable",
                "evidence": {"http_status": 400, "reason": "not_enrolled"},
                "source": "probe",
                "checked_at": "2026-07-13T12:00:00",
            }
        )
        report.record_doc("doc-1", "e2e-gdocs-review-x")
        report.mark_doc_cleaned("doc-1", "trash")
        report.record_error_shape(
            "probe", 400, "Unknown name 'acceptSuggestion'", "unavailable"
        )
        report.note("hello note")

        target = report.write(tmp_path / "last_run.md")
        text = target.read_text()
        assert "user@example.com" in text
        assert "| e2e_ga | 1 | 0 | 0 |" in text
        assert "availability: **unavailable**" in text
        assert "not_enrolled" in text
        assert "| doc-1 | e2e-gdocs-review-x | yes | trash |" in text
        assert "Unknown name 'acceptSuggestion'" in text
        assert "hello note" in text

    def test_no_creds_report(self):
        report = RunReport()
        report.set_gating_note("E2E SKIPPED - no token")
        report.observe("a", {"e2e_ga"}, "setup", "skipped", "E2E SKIPPED - no token")
        text = report.render_markdown()
        assert "token identity: none" in text
        assert "E2E SKIPPED - no token" in text


class TestResolveReportPath:
    def test_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("E2E_RUN_REPORT_PATH", str(tmp_path / "r.md"))
        assert resolve_report_path() == tmp_path / "r.md"

    def test_default_under_e2e(self, monkeypatch):
        monkeypatch.delenv("E2E_RUN_REPORT_PATH", raising=False)
        assert resolve_report_path().name == "last_run.md"
        assert resolve_report_path().parent.name == "e2e"
