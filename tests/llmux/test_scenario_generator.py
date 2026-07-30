"""Unit tests for the generator itself.

The generator's job is to be *distrustful*: it builds each scenario twice
(model path and tool path) and refuses to write anything if the two disagree
or if the oracle does not grade clean. These tests poke holes in both guards,
because a guard that has never fired is a guard nobody knows is wired up.
"""

from __future__ import annotations

import json

import pytest

from llmux.scenarios import generate as gen
from llmux.scenarios.catalog import Scenario
from llmux.scenarios.primitives import (
    SeedBuilder,
    add,
    after,
    remove,
    seeded_backend,
    span,
)
from llmux.scenarios.steps import Accept, Reject


def _scenario(brief_extra: str = "") -> Scenario:
    b = SeedBuilder(base_text="one two three\n", document_id="d-gen")
    b.move("cut", remove("alice", span(" two")))
    b.move("ins", add("bob", after("three"), " four"))
    _, doc = seeded_backend(b.seed_spec())
    return Scenario(
        id="unit-fixture",
        tier="easy",
        difficulty=1,
        tags=["unit"],
        authors=["alice", "bob"],
        seed=b.seed_spec(),
        document_id="d-gen",
        brief=f"# Fixture\n\nDocument ID: `d-gen`\n{brief_extra}\n",
        steps=[Accept(b.named["cut"]), Reject(b.named["ins"])],
        n_suggestions=len(doc.registry),
        extra_checks=[{"check": "suggestion_count", "equals": 0}],
    )


class TestBuild:
    def test_produces_the_contract_files(self):
        files = gen.build(_scenario())
        assert set(files) == {
            "seed.json",
            "expected.json",
            "meta.json",
            "solution.json",
            "brief.md",
            "grade.py",
        }
        expected = files["expected.json"]
        assert set(expected) == {
            "document_id",
            "final_text",
            "surviving_suggestion_ids",
            "resolved",
            "thread_expectations",
            "invariant_checks",
        }
        assert expected["final_text"] == "one three\n"
        assert expected["surviving_suggestion_ids"] == []
        assert set(expected["resolved"].values()) == {"accepted", "rejected"}
        meta = files["meta.json"]
        assert meta["steps"] == len(files["solution.json"]) == 2
        assert 1 <= meta["difficulty"] <= 5

    def test_ground_truth_comes_from_the_model_not_the_steps(self):
        """Nothing in the scenario definition states the end text; it falls
        out of §7 applied to the seeded document."""
        scenario = _scenario()
        assert "one three" not in json.dumps(scenario.seed)
        assert gen.build(scenario)["expected.json"]["final_text"] == "one three\n"

    def test_empty_seed_is_rejected(self):
        scenario = _scenario()
        scenario.seed["documents"][0]["suggestions"] = []
        scenario.steps = []
        with pytest.raises(gen.GenerationError, match="no suggestions"):
            gen.build(scenario)

    def test_model_and_api_paths_must_agree(self, monkeypatch):
        """Silence the tool path and the cross-check must notice."""
        monkeypatch.setattr(gen, "run_solution", lambda *a, **k: None)
        with pytest.raises(gen.GenerationError, match="disagree"):
            gen.build(_scenario())

    def test_a_dirty_oracle_verdict_fails_the_build(self, monkeypatch):
        monkeypatch.setattr(
            gen,
            "grade_against",
            lambda *a, **k: {"pass": False, "score": 0.5, "failures": ["nope"]},
        )
        with pytest.raises(gen.GenerationError, match="does not grade clean"):
            gen.build(_scenario())

    def test_step_on_a_dead_suggestion_is_a_build_error(self):
        """Resolving A can garbage-collect B (I2); the oracle has to be
        ordered so it never addresses a dead id."""
        scenario = _scenario()
        first = scenario.steps[0].suggestion_id
        scenario.steps = [Accept(first), Accept(first)]
        with pytest.raises(gen.GenerationError, match="no longer live"):
            gen.build(scenario)


class TestBriefHygiene:
    def test_leaked_suggestion_id_is_caught(self):
        scenario = _scenario(brief_extra="accept sug.alice.1 please")
        _, doc = seeded_backend(scenario.seed)
        problems = gen.check_brief(scenario, sorted(doc.registry))
        assert any("suggestion id" in p for p in problems)

    def test_missing_document_id_is_caught(self):
        scenario = _scenario()
        scenario.brief = "# Fixture\n\nno document here\n"
        assert gen.check_brief(scenario, []) == ["brief does not state the document id"]

    def test_clean_brief_passes(self):
        scenario = _scenario()
        _, doc = seeded_backend(scenario.seed)
        assert gen.check_brief(scenario, sorted(doc.registry)) == []


class TestWrite:
    def test_writes_json_and_text_side_by_side(self, tmp_path):
        scenario = _scenario()
        target = gen.write(scenario, gen.build(scenario), tmp_path)
        assert (target / "brief.md").read_text(encoding="utf-8").startswith("# Fixture")
        assert json.loads((target / "expected.json").read_text(encoding="utf-8"))
        assert "def grade(backend" in (target / "grade.py").read_text(encoding="utf-8")
