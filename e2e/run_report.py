"""Run-report collector for the e2e suite.

Collects outcomes, token identity, preview classification evidence,
scratch-doc hygiene and observed API error shapes, then renders
``e2e/last_run.md`` (path overridable via E2E_RUN_REPORT_PATH). This is
the artifact forwarded to the client requester after a real run.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

MARKERS = ("e2e_ga", "e2e_preview")

DEFAULT_REPORT_PATH = Path(__file__).resolve().parent / "last_run.md"


def resolve_report_path() -> Path:
    override = (os.getenv("E2E_RUN_REPORT_PATH") or "").strip()
    return Path(override).expanduser() if override else DEFAULT_REPORT_PATH


class RunReport:
    def __init__(self) -> None:
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S %z")
        self.identity: dict[str, Any] | None = None
        self.gating_note: str | None = None
        self.preview: dict[str, Any] | None = None
        self.outcomes: dict[str, dict[str, Any]] = {}
        self.docs: dict[str, dict[str, Any]] = {}
        self.error_shapes: list[dict[str, Any]] = []
        self.notes: list[str] = []

    # -- feeding ---------------------------------------------------------
    def set_identity(self, email: str, credentials_dir: str, verified_via: str) -> None:
        self.identity = {
            "email": email,
            "credentials_dir": credentials_dir,
            "verified_via": verified_via,
        }

    def set_gating_note(self, note: str) -> None:
        self.gating_note = note

    def set_preview_classification(self, preview: dict[str, Any]) -> None:
        self.preview = dict(preview)

    def observe(
        self,
        nodeid: str,
        markers: set[str],
        when: str,
        outcome: str,
        skip_reason: str | None = None,
    ) -> None:
        """Fold one pytest phase report into the per-test outcome.

        Precedence: failed > skipped > passed; only the call phase can
        mark a test passed.
        """
        entry = self.outcomes.setdefault(
            nodeid, {"markers": set(markers), "outcome": None, "skip_reason": None}
        )
        if outcome == "failed":
            entry["outcome"] = "failed"
        elif outcome == "skipped" and entry["outcome"] != "failed":
            entry["outcome"] = "skipped"
            entry["skip_reason"] = skip_reason
        elif outcome == "passed" and when == "call" and entry["outcome"] is None:
            entry["outcome"] = "passed"

    def record_doc(self, doc_id: str, title: str) -> None:
        self.docs[doc_id] = {"title": title, "cleaned": False, "method": None}

    def mark_doc_cleaned(self, doc_id: str, method: str) -> None:
        if doc_id in self.docs:
            self.docs[doc_id]["cleaned"] = True
            self.docs[doc_id]["method"] = method

    def record_error_shape(
        self,
        label: str,
        status: int | None,
        message: str,
        classification: str | None = None,
    ) -> None:
        self.error_shapes.append(
            {
                "label": label,
                "status": status,
                "message": message[:500],
                "classification": classification,
            }
        )

    def note(self, text: str) -> None:
        self.notes.append(text)

    # -- rendering -------------------------------------------------------
    def marker_counts(self) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        for entry in self.outcomes.values():
            outcome = entry["outcome"] or "unknown"
            buckets = [m for m in MARKERS if m in entry["markers"]] or ["unmarked"]
            for bucket in buckets:
                per = counts.setdefault(
                    bucket, {"passed": 0, "failed": 0, "skipped": 0, "unknown": 0}
                )
                per[outcome] = per.get(outcome, 0) + 1
        return counts

    def render_markdown(self) -> str:
        lines = [
            "# e2e last run",
            "",
            f"- started: {self.started_at}",
            f"- finished: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        ]
        if self.identity:
            lines.append(
                f"- token identity: **{self.identity['email']}** "
                f"(verified via {self.identity['verified_via']}; "
                f"store: {self.identity['credentials_dir']})"
            )
        else:
            lines.append("- token identity: none (suite skipped - no credentials)")
        if self.gating_note:
            lines += ["", "## Gating", "", "```", self.gating_note, "```"]

        lines += ["", "## Results by marker", ""]
        counts = self.marker_counts()
        if counts:
            lines.append("| marker | passed | failed | skipped |")
            lines.append("|---|---|---|---|")
            for marker in [*MARKERS, "unmarked"]:
                if marker in counts:
                    c = counts[marker]
                    lines.append(
                        f"| {marker} | {c['passed']} | {c['failed']} | {c['skipped']} |"
                    )
        else:
            lines.append("no e2e tests ran.")

        skip_reasons = sorted(
            {
                e["skip_reason"]
                for e in self.outcomes.values()
                if e["outcome"] == "skipped" and e["skip_reason"]
            }
        )
        if skip_reasons:
            lines += ["", "### Skip reasons", ""]
            for reason in skip_reasons:
                lines += ["```", str(reason), "```"]

        lines += ["", "## Preview classification", ""]
        if self.preview:
            lines.append(f"- availability: **{self.preview.get('availability')}**")
            lines.append(f"- source: {self.preview.get('source')}")
            lines.append(f"- checked_at: {self.preview.get('checked_at')}")
            evidence = self.preview.get("evidence")
            if evidence:
                lines += ["- evidence:", "```json"]
                import json as _json

                lines.append(_json.dumps(evidence, indent=2, ensure_ascii=False))
                lines.append("```")
        else:
            lines.append("not probed this run.")

        lines += ["", "## Scratch documents", ""]
        if self.docs:
            lines.append("| doc id | title | cleaned | method |")
            lines.append("|---|---|---|---|")
            for doc_id, info in self.docs.items():
                lines.append(
                    f"| {doc_id} | {info['title']} | "
                    f"{'yes' if info['cleaned'] else 'NO'} | {info['method'] or '-'} |"
                )
        else:
            lines.append("none created.")

        if self.error_shapes:
            lines += ["", "## Observed API error shapes (classifier evidence)", ""]
            for shape in self.error_shapes:
                lines.append(
                    f"- **{shape['label']}** (HTTP {shape['status']}, "
                    f"classified: {shape['classification'] or 'n/a'}):"
                )
                lines += [
                    "  ```",
                    "  " + shape["message"].replace("\n", "\n  "),
                    "  ```",
                ]

        if self.notes:
            lines += ["", "## Notes", ""]
            lines += [f"- {note}" for note in self.notes]

        lines.append("")
        return "\n".join(lines)

    def write(self, path: Path | None = None) -> Path:
        target = path or resolve_report_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.render_markdown(), encoding="utf-8")
        return target


#: Session-wide singleton fed by e2e/conftest.py hooks.
REPORT = RunReport()
