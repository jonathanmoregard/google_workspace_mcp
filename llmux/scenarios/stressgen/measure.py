"""Measure the stress corpus: context cost, and whether the score is a curve.

    uv run python -m llmux.scenarios.stressgen.measure
    uv run python -m llmux.scenarios.stressgen.measure --markdown

Two questions, both of which have to be answered before spending tokens on
agent runs.

**What does it cost to look at the work?** At 120 suggestions the
interesting failure is not tool syntax, it is context: the agent has to hold
the whole review set in its head to apply an ordered rule list to it.
:func:`context_profile` calls the real read tools against each seeded
scenario and reports how large their output is, so "the read does not fit"
stops being a hunch.

**Does partial credit actually grade partially?** A benchmark whose scoring
is all-or-nothing tells you a run failed, not where. :func:`score_profile`
replays the oracle assignment with a controlled number of wrong decisions
and reports the resulting score, which is the calibration curve for every
number the runner will later produce.

Token counts are estimates. No tokenizer ships with this repo, so the
character count is the measurement and the token figure is
``characters / 3.6`` -- the ratio BPE tokenizers land on for JSON-wrapped
English. It is stated as an estimate everywhere it appears.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from mockdocs.model import MockDoc

from llmux.scenarios.grading import grade_against
from llmux.scenarios.oracle import ORACLE_EMAIL, tool_table
from llmux.scenarios.primitives import ordered_suggestion_ids, seeded_backend
from llmux.scenarios.stressgen.build import STRESS_ROOT
from llmux.scenarios.stressgen.catalog import STRESS_SPECS, decisions_for, walk_for

#: Characters per token. A stated approximation, not a measurement.
CHARS_PER_TOKEN = 3.6


def est_tokens(text: str) -> int:
    return round(len(text) / CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# context cost
# ---------------------------------------------------------------------------


@dataclass
class ContextProfile:
    scenario_id: str
    n_suggestions: int
    brief_chars: int
    list_chars: int
    review_view_chars: int
    list_records: int
    per_record_chars: float
    #: The pre-M8 shape of each read: every field, every card, one response.
    #: Kept alongside the defaults so the report is a before/after rather
    #: than a claim.
    list_full_chars: int = 0
    review_view_full_chars: int = 0

    @property
    def total_read_chars(self) -> int:
        return self.brief_chars + self.list_chars + self.review_view_chars

    @property
    def total_read_full_chars(self) -> int:
        return self.brief_chars + self.list_full_chars + self.review_view_full_chars

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "n_suggestions": self.n_suggestions,
            "brief_chars": self.brief_chars,
            "brief_tokens_est": round(self.brief_chars / CHARS_PER_TOKEN),
            "list_document_suggestions_chars": self.list_chars,
            "list_document_suggestions_tokens_est": round(
                self.list_chars / CHARS_PER_TOKEN
            ),
            "list_document_suggestions_full_chars": self.list_full_chars,
            "list_document_suggestions_full_tokens_est": round(
                self.list_full_chars / CHARS_PER_TOKEN
            ),
            "get_doc_review_view_chars": self.review_view_chars,
            "get_doc_review_view_tokens_est": round(
                self.review_view_chars / CHARS_PER_TOKEN
            ),
            "get_doc_review_view_full_chars": self.review_view_full_chars,
            "get_doc_review_view_full_tokens_est": round(
                self.review_view_full_chars / CHARS_PER_TOKEN
            ),
            "records": self.list_records,
            "chars_per_record": round(self.per_record_chars, 1),
            "one_read_of_each_tokens_est": round(
                self.total_read_chars / CHARS_PER_TOKEN
            ),
            "one_read_of_each_full_tokens_est": round(
                self.total_read_full_chars / CHARS_PER_TOKEN
            ),
        }


async def _read_outputs(backend: Any, document_id: str) -> dict[str, str]:
    """Both read tools, at their defaults AND at ``fields='full'``.

    The default is what an agent gets; the full shape is what the tool
    returned before the narrowing parameters existed. Reporting both is the
    only way the reduction claim is checkable.
    """
    tools = tool_table()
    common = {
        "service": backend.docs_service(),
        "user_google_email": ORACLE_EMAIL,
        "document_id": document_id,
    }
    return {
        "list": await tools["list_document_suggestions"](**common),
        "list_full": await tools["list_document_suggestions"](
            **common, fields="full", page_size=1000
        ),
        "view": await tools["get_doc_review_view"](**common),
        "view_full": await tools["get_doc_review_view"](**common, fields="full"),
    }


def context_profile(scenario_dir: Path) -> ContextProfile:
    """Size of everything the agent must read before it can decide anything."""
    seed = json.loads((scenario_dir / "seed.json").read_text(encoding="utf-8"))
    meta = json.loads((scenario_dir / "meta.json").read_text(encoding="utf-8"))
    brief = (scenario_dir / "brief.md").read_text(encoding="utf-8")
    backend, _doc = seeded_backend(seed)
    outputs = asyncio.run(_read_outputs(backend, meta["document_id"]))
    listed = outputs["list"]

    # Defensive: the write/read tools are being enriched concurrently, so read
    # what is needed with .get() and never assume a key set.
    try:
        payload = json.loads(listed)
    except ValueError:
        payload = {}
    records = payload.get("suggestions")
    if not isinstance(records, list):
        records = payload.get("records") if isinstance(payload, dict) else None
    count = (
        len(records)
        if isinstance(records, list)
        else int(payload.get("suggestion_count") or 0)
    )
    return ContextProfile(
        scenario_id=meta.get("id", scenario_dir.name),
        n_suggestions=int(meta.get("n_suggestions") or 0),
        brief_chars=len(brief),
        list_chars=len(listed),
        review_view_chars=len(outputs["view"]),
        list_records=count,
        per_record_chars=(len(listed) / count) if count else 0.0,
        list_full_chars=len(outputs["list_full"]),
        review_view_full_chars=len(outputs["view_full"]),
    )


# ---------------------------------------------------------------------------
# score calibration
# ---------------------------------------------------------------------------


def _apply(doc: MockDoc, decisions: dict[str, str]) -> None:
    """Resolve in card order, skipping anything already collected (§11.1)."""
    for sid in ordered_suggestion_ids(doc):
        action = decisions.get(sid)
        if action is None or sid not in doc.registry:
            continue
        if action == "accept":
            doc.accept(sid)
        else:
            doc.reject(sid)


def _graded(seed: dict[str, Any], expected: dict[str, Any], decisions: dict[str, str]):
    backend, doc = seeded_backend(seed)
    _apply(doc, decisions)
    return grade_against(backend, expected)


def score_profile(scenario_dir: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """Score of the intended answer, of shortcuts, and of N wrong calls."""
    seed = json.loads((scenario_dir / "seed.json").read_text(encoding="utf-8"))
    expected = json.loads((scenario_dir / "expected.json").read_text(encoding="utf-8"))
    _doc, decisions, _task = decisions_for(spec)
    _backend, live = seeded_backend(seed)
    all_ids = sorted(live.registry)

    rows: dict[str, float] = {}
    rows["oracle"] = _graded(seed, expected, dict(decisions))["score"]
    rows["do_nothing"] = _graded(seed, expected, {})["score"]
    rows["accept_all"] = _graded(seed, expected, {s: "accept" for s in all_ids})["score"]
    rows["reject_all"] = _graded(seed, expected, {s: "reject" for s in all_ids})["score"]
    rows["resolve_all_as_intended_never_defer"] = _graded(
        seed,
        expected,
        {s: decisions.get(s, "accept") for s in all_ids},
    )["score"]

    rng = random.Random(20260730)
    decided = sorted(decisions)
    for wrong in (1, 5, 10, 20, 40):
        if wrong > len(decided):
            continue
        mutated = dict(decisions)
        for sid in rng.sample(decided, wrong):
            mutated[sid] = "reject" if mutated[sid] == "accept" else "accept"
        rows[f"oracle_with_{wrong}_wrong_calls"] = _graded(seed, expected, mutated)[
            "score"
        ]
    return {k: round(v, 4) for k, v in rows.items()}


# ---------------------------------------------------------------------------
# realised review shape
# ---------------------------------------------------------------------------


def walk_profile(spec: dict[str, Any]) -> dict[str, Any]:
    walk = walk_for(spec)
    doc, decisions, task = decisions_for(spec)
    return {
        "document": walk.document.key,
        "words": walk.document.word_count,
        "suggestions": len(doc.registry),
        "comment_threads": walk.comments,
        "nested_insertions": walk.nested,
        "cross_author_overlaps": walk.overlaps,
        "accept": sum(1 for a in decisions.values() if a == "accept"),
        "reject": sum(1 for a in decisions.values() if a == "reject"),
        "leave_pending": len(doc.registry) - len(decisions),
        "rules": len(task.clauses),
        "edit_kinds": dict(walk.kind_histogram().most_common()),
        "reviewers": dict(walk.reviewer_histogram().most_common()),
        "edit_sizes": dict(walk.size_histogram().most_common()),
    }


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def collect(root: Path = STRESS_ROOT) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in STRESS_SPECS:
        directory = root / spec["scenario_id"]
        if not (directory / "meta.json").exists():
            continue
        out.append(
            {
                "context": context_profile(directory).as_dict(),
                "scores": score_profile(directory, spec),
                "walk": walk_profile(spec),
            }
        )
    return out


def markdown(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = ["# Stress corpus measurements", ""]
    lines.append(
        f"Token figures are estimates: characters / {CHARS_PER_TOKEN} "
        "(no tokenizer ships with this repo). Character counts are exact."
    )
    lines.append("")
    lines.append("## Context cost of one read")
    lines.append("")
    lines.append(
        "`default` is what an agent gets today (`fields='summary'` for "
        "`list_document_suggestions`, `fields='text'` for "
        "`get_doc_review_view`); `full` is every field of every card in one "
        "response, which is what both tools returned before they had "
        "narrowing parameters."
    )
    lines.append("")
    lines.append(
        "| scenario | cards | brief (chars) | `list` full (chars) | "
        "`list` default (chars) | est. tokens | cut | "
        "`review_view` full (chars) | `review_view` default (chars) | "
        "est. tokens | cut | one read of each: full -> default (est. tokens) |"
    )
    lines.append(
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: "
        "| ---: | ---: |"
    )
    for row in rows:
        c = row["context"]

        def cut(full: int, now: int) -> str:
            return f"{(1 - now / full) * 100:.1f}%" if full else "-"

        lines.append(
            f"| `{c['scenario_id']}` | {c['n_suggestions']} | {c['brief_chars']:,} | "
            f"{c['list_document_suggestions_full_chars']:,} | "
            f"{c['list_document_suggestions_chars']:,} | "
            f"{c['list_document_suggestions_tokens_est']:,} | "
            f"{cut(c['list_document_suggestions_full_chars'], c['list_document_suggestions_chars'])} | "
            f"{c['get_doc_review_view_full_chars']:,} | "
            f"{c['get_doc_review_view_chars']:,} | "
            f"{c['get_doc_review_view_tokens_est']:,} | "
            f"{cut(c['get_doc_review_view_full_chars'], c['get_doc_review_view_chars'])} | "
            f"{c['one_read_of_each_full_tokens_est']:,} -> "
            f"{c['one_read_of_each_tokens_est']:,} |"
        )
    lines.append("")
    lines.append("## Score calibration")
    lines.append("")
    keys = sorted({k for row in rows for k in row["scores"]})
    lines.append("| strategy | " + " | ".join(r["context"]["scenario_id"].replace("stress-", "") for r in rows) + " |")
    lines.append("| --- | " + " | ".join("---:" for _ in rows) + " |")
    for key in ["oracle", "do_nothing", "accept_all", "reject_all",
                "resolve_all_as_intended_never_defer"] + [
        k for k in keys if k.startswith("oracle_with_")
    ]:
        cells = [f"{row['scores'].get(key, float('nan')):.3f}" for row in rows]
        lines.append(f"| `{key}` | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("## Realised review shape")
    lines.append("")
    for row in rows:
        w = row["walk"]
        lines.append(f"### `{row['context']['scenario_id']}`")
        lines.append("")
        lines.append(
            f"{w['document']} ({w['words']:,} words), {w['suggestions']} cards, "
            f"{w['comment_threads']} comment threads, {w['nested_insertions']} "
            f"nested insertions, {w['cross_author_overlaps']} cross-author "
            f"overlaps. Task: {w['rules']} ordered rules -> accept "
            f"{w['accept']}, reject {w['reject']}, leave pending "
            f"{w['leave_pending']}."
        )
        lines.append("")
        lines.append("- edit kinds: " + ", ".join(f"{k} {v}" for k, v in w["edit_kinds"].items()))
        lines.append("- reviewers: " + ", ".join(f"{k} {v}" for k, v in w["reviewers"].items()))
        lines.append("- sizes: " + ", ".join(f"{k} {v}" for k, v in w["edit_sizes"].items()))
        lines.append("")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(STRESS_ROOT))
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--out", default=None, help="write the report to this path")
    args = parser.parse_args(argv)

    rows = collect(Path(args.root))
    text = markdown(rows) if args.markdown or args.out else json.dumps(rows, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
