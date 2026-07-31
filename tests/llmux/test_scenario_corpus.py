"""The committed corpus is solvable, correctly graded, and reproducible.

This is the mandatory gate from the chunk brief, run as an ordinary test so
it cannot be skipped: every scenario in ``llmux/scenarios/generated`` is
replayed through the real MCP tools and must grade 1.0, and the untouched
document must not.

The regeneration test is the other half: the corpus in git has to be exactly
what ``python -m llmux.scenarios.generate`` produces today. If it drifts --
because a mockdocs behaviour changed under it, or because someone edited a
generated file by hand -- every LLM run against the corpus is being graded
against stale ground truth, which is worse than no corpus at all.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from llmux.scenarios import generate as gen
from llmux.scenarios.validate import GENERATED, validate_all, validate_scenario

SCENARIO_DIRS = sorted(p for p in GENERATED.iterdir() if (p / "meta.json").exists())

CONTRACT_FILES = ("seed.json", "brief.md", "expected.json", "grade.py", "meta.json")
META_KEYS = {"id", "difficulty", "steps", "tags", "authors", "n_suggestions"}
EXPECTED_KEYS = {
    "final_text",
    "surviving_suggestion_ids",
    "resolved",
    "thread_expectations",
    "invariant_checks",
}


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_corpus_is_not_empty():
    assert len(SCENARIO_DIRS) >= 12, "the first batch is meant to be 12-16 scenarios"


@pytest.mark.parametrize("scenario_dir", SCENARIO_DIRS, ids=lambda p: p.name)
def test_scenario_is_solvable_and_graded_right(scenario_dir):
    result = validate_scenario(scenario_dir)
    assert result.ok, result.problems
    assert result.score == 1.0


@pytest.mark.parametrize("scenario_dir", SCENARIO_DIRS, ids=lambda p: p.name)
def test_scenario_meets_the_file_contract(scenario_dir):
    for name in CONTRACT_FILES:
        assert (scenario_dir / name).exists(), f"{scenario_dir.name} lacks {name}"
    meta = _json(scenario_dir / "meta.json")
    assert META_KEYS <= set(meta)
    assert meta["id"] == scenario_dir.name
    assert isinstance(meta["difficulty"], int) and 1 <= meta["difficulty"] <= 5
    assert meta["n_suggestions"] >= 2
    expected = _json(scenario_dir / "expected.json")
    assert EXPECTED_KEYS <= set(expected)
    assert expected["document_id"] == meta["document_id"]


@pytest.mark.parametrize("scenario_dir", SCENARIO_DIRS, ids=lambda p: p.name)
def test_brief_forces_discovery(scenario_dir):
    """No suggestion or comment ids in the brief: the agent has to read."""
    brief = (scenario_dir / "brief.md").read_text(encoding="utf-8")
    assert "sug." not in brief
    assert "comment." not in brief
    assert _json(scenario_dir / "meta.json")["document_id"] in brief


def test_ladder_covers_every_tier():
    tiers = Counter(_json(p / "meta.json")["tier"] for p in SCENARIO_DIRS)
    assert set(tiers) == {"easy", "medium", "hard", "adversarial"}
    for tier, count in tiers.items():
        assert count >= 3, f"{tier} tier has only {count} scenario(s)"


def test_hard_and_adversarial_tiers_are_actually_harder():
    by_tier = {}
    for p in SCENARIO_DIRS:
        meta = _json(p / "meta.json")
        by_tier.setdefault(meta["tier"], []).append(meta)
    assert max(m["n_suggestions"] for m in by_tier["hard"]) >= 8
    assert all(m["difficulty"] <= 2 for m in by_tier["easy"])
    assert all(m["difficulty"] == 5 for m in by_tier["adversarial"])
    multi_author = [m for m in by_tier["hard"] if len(m["authors"]) >= 3]
    assert multi_author, "the hard tier is meant to be multi-author"


def test_the_traps_are_present():
    """The adversarial mechanisms the chunk brief asks for, by tag."""
    tags = {t for p in SCENARIO_DIRS for t in _json(p / "meta.json")["tags"]}
    for required in (
        "stale-index",
        "no-op",
        "both-marks",
        "nested-insertion",
        "overlap",
        "merge",
        "decoy",
        "utf16",
        "emoji",
        "two-phase",
        "anchor",
    ):
        assert required in tags, f"no scenario exercises {required!r}"


@pytest.mark.parametrize("scenario_dir", SCENARIO_DIRS, ids=lambda p: p.name)
def test_seed_loads_the_way_the_server_loads_it(scenario_dir, monkeypatch):
    """``MOCKDOCS_SEED`` is the contract with the runner: the seed has to come
    up through ``mockdocs/serve.py``'s own construction path, not just through
    ``FakeBackend.seed`` called by hand."""
    from mockdocs import serve

    monkeypatch.setenv("MOCKDOCS_SEED", str(scenario_dir / "seed.json"))
    monkeypatch.delenv("MOCKDOCS_NOT_ENROLLED", raising=False)
    monkeypatch.delenv("MOCKDOCS_FAIL_COMMENTS", raising=False)

    backend = serve.build_backend()
    meta = _json(scenario_dir / "meta.json")
    doc = backend.get_document(meta["document_id"])
    assert len(doc.registry) == meta["n_suggestions"]
    assert backend.me == meta["me"]
    assert not backend.not_enrolled
    # The seed replays §5 operations, so the ids the corpus refers to are the
    # ids the server hands the agent.
    expected = _json(scenario_dir / "expected.json")
    referenced = set(expected["resolved"]) | set(expected["surviving_suggestion_ids"])
    assert referenced <= set(doc.registry) | {
        s for s in referenced if s.startswith("sug.reviewer.")
    }


def test_validate_all_passes():
    results = validate_all()
    failed = [(r.scenario_id, r.problems) for r in results if not r.ok]
    assert not failed
    assert all(r.score == 1.0 for r in results)


def test_corpus_is_reproducible(tmp_path):
    """Regenerating must reproduce the committed bytes exactly."""
    gen.generate(root=tmp_path)
    regenerated = sorted(p for p in tmp_path.iterdir() if p.is_dir())
    assert [p.name for p in regenerated] == [p.name for p in SCENARIO_DIRS]
    for fresh, committed in zip(regenerated, SCENARIO_DIRS):
        for name in (*CONTRACT_FILES, "solution.json"):
            assert (fresh / name).read_text(encoding="utf-8") == (
                committed / name
            ).read_text(encoding="utf-8"), f"{committed.name}/{name} is stale"
