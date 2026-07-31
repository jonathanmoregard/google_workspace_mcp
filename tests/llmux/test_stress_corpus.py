"""The stress corpus is solvable, correctly graded, and discriminating.

Same gate as ``test_scenario_corpus.py`` -- every scenario is replayed
through the real MCP tools and must grade 1.0, the untouched document must
not, and the committed bytes must be exactly what the generator produces
today -- plus the checks that only matter at this scale:

- **partial credit is actually partial.** A 120-card task graded
  all-or-nothing tells you a run failed, not where. The score of a run that
  gets one call wrong has to sit strictly between "wrong" and "right", and
  it has to move monotonically as more calls go wrong.
- **the shortcuts lose.** Accept-all, reject-all and do-nothing must all
  score well below the intended answer, or the task does not require the
  discrimination it claims to.
- **the base text is prose.** The whole point of this corpus is that it is
  fair against reality, which stops being true the moment someone replaces
  a document with filler.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path

import pytest

from llmux.scenarios.grading import grade_against
from llmux.scenarios.primitives import ordered_suggestion_ids, seeded_backend
from llmux.scenarios.stressgen import build as stress_build
from llmux.scenarios.stressgen import prose
from llmux.scenarios.stressgen.catalog import STRESS_SPECS, decisions_for
from llmux.scenarios.stressgen.edits import KINDS, all_sentences, candidates
from llmux.scenarios.stressgen.invariants import project, witness
from llmux.scenarios.validate import validate_scenario

STRESS_ROOT = stress_build.STRESS_ROOT
SCENARIO_DIRS = sorted(p for p in STRESS_ROOT.iterdir() if (p / "meta.json").exists())
CONTRACT_FILES = ("seed.json", "brief.md", "expected.json", "grade.py", "meta.json")


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# the corpus exists and covers the ladder the brief asks for
# ---------------------------------------------------------------------------


def test_the_stress_ladder_is_present():
    sizes = sorted(_json(p / "meta.json")["n_suggestions"] for p in SCENARIO_DIRS)
    assert sizes == [30, 60, 90, 120], sizes
    assert max(sizes) >= 100, "the corpus must reach 100+ suggestions"


@pytest.mark.parametrize("scenario_dir", SCENARIO_DIRS, ids=lambda p: p.name)
def test_scenario_meets_the_file_contract(scenario_dir):
    for name in CONTRACT_FILES:
        assert (scenario_dir / name).exists(), f"{scenario_dir.name} lacks {name}"
    meta = _json(scenario_dir / "meta.json")
    assert meta["id"] == scenario_dir.name
    assert meta["tier"] == "stress"
    assert 1 <= meta["difficulty"] <= 5
    assert len(meta["authors"]) == 5, "the stress corpus is a five-reviewer panel"
    expected = _json(scenario_dir / "expected.json")
    assert expected["document_id"] == meta["document_id"]


@pytest.mark.parametrize("scenario_dir", SCENARIO_DIRS, ids=lambda p: p.name)
def test_brief_forces_discovery(scenario_dir):
    brief = (scenario_dir / "brief.md").read_text(encoding="utf-8")
    assert "sug." not in brief
    assert "comment." not in brief
    assert _json(scenario_dir / "meta.json")["document_id"] in brief


# ---------------------------------------------------------------------------
# solvable and correctly graded -- the non-negotiable gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario_dir", SCENARIO_DIRS, ids=lambda p: p.name)
def test_scenario_is_solvable_and_graded_right(scenario_dir):
    result = validate_scenario(scenario_dir)
    assert result.ok, result.problems
    assert result.score == 1.0


@pytest.mark.parametrize("scenario_dir", SCENARIO_DIRS, ids=lambda p: p.name)
def test_seed_loads_the_way_the_server_loads_it(scenario_dir, monkeypatch):
    from mockdocs import serve

    monkeypatch.setenv("MOCKDOCS_SEED", str(scenario_dir / "seed.json"))
    monkeypatch.delenv("MOCKDOCS_NOT_ENROLLED", raising=False)
    monkeypatch.delenv("MOCKDOCS_FAIL_COMMENTS", raising=False)

    backend = serve.build_backend()
    meta = _json(scenario_dir / "meta.json")
    doc = backend.get_document(meta["document_id"])
    assert len(doc.registry) == meta["n_suggestions"]
    assert backend.me == meta["me"]
    doc.check_invariants()


def test_corpus_is_reproducible(tmp_path):
    """Regenerating must reproduce the committed bytes exactly."""
    stress_build.generate(root=tmp_path)
    regenerated = sorted(p for p in tmp_path.iterdir() if p.is_dir())
    assert [p.name for p in regenerated] == [p.name for p in SCENARIO_DIRS]
    for fresh, committed in zip(regenerated, SCENARIO_DIRS):
        for name in (*CONTRACT_FILES, "solution.json"):
            assert (fresh / name).read_text(encoding="utf-8") == (
                committed / name
            ).read_text(encoding="utf-8"), f"{committed.name}/{name} is stale"


# ---------------------------------------------------------------------------
# grading is a curve, and the shortcuts lose
# ---------------------------------------------------------------------------


def _apply(doc, decisions):
    for sid in ordered_suggestion_ids(doc):
        action = decisions.get(sid)
        if action is None or sid not in doc.registry:
            continue
        (doc.accept if action == "accept" else doc.reject)(sid)


def _score(seed, expected, decisions) -> float:
    backend, doc = seeded_backend(seed)
    _apply(doc, decisions)
    return grade_against(backend, expected)["score"]


@pytest.mark.parametrize("spec", STRESS_SPECS, ids=lambda s: s["scenario_id"])
def test_shortcut_strategies_score_far_below_the_answer(spec):
    directory = STRESS_ROOT / spec["scenario_id"]
    seed = _json(directory / "seed.json")
    expected = _json(directory / "expected.json")
    _doc, decisions, _task = decisions_for(spec)
    _backend, live = seeded_backend(seed)
    ids = sorted(live.registry)

    assert _score(seed, expected, dict(decisions)) == 1.0
    for label, shortcut in (
        ("do nothing", {}),
        ("accept all", {s: "accept" for s in ids}),
        ("reject all", {s: "reject" for s in ids}),
        ("never defer", {s: decisions.get(s, "accept") for s in ids}),
    ):
        score = _score(seed, expected, shortcut)
        assert score < 0.8, f"{label} scores {score:.3f}: the task is too easy"


@pytest.mark.parametrize("spec", STRESS_SPECS, ids=lambda s: s["scenario_id"])
def test_score_degrades_monotonically_with_wrong_calls(spec):
    """The point of the corpus: find *where* it breaks, not merely that it did."""
    directory = STRESS_ROOT / spec["scenario_id"]
    seed = _json(directory / "seed.json")
    expected = _json(directory / "expected.json")
    _doc, decisions, _task = decisions_for(spec)
    decided = sorted(decisions)

    rng = random.Random(4242)
    scores = []
    for wrong in (0, 2, 4, 8):
        mutated = dict(decisions)
        for sid in rng.sample(decided, min(wrong, len(decided))):
            mutated[sid] = "reject" if mutated[sid] == "accept" else "accept"
        scores.append(_score(seed, expected, mutated))
    assert scores[0] == 1.0
    assert all(later <= earlier for earlier, later in zip(scores, scores[1:])), scores
    assert scores[-1] < scores[0], scores
    assert scores[-1] > 0.5, f"eight wrong calls should not zero the score: {scores}"


@pytest.mark.parametrize("scenario_dir", SCENARIO_DIRS, ids=lambda p: p.name)
def test_grading_is_per_suggestion(scenario_dir):
    """Almost every card carries its own witness, so credit is per card."""
    expected = _json(scenario_dir / "expected.json")
    meta = _json(scenario_dir / "meta.json")
    checks = expected["invariant_checks"]
    witnesses = [c for c in checks if c.get("check") == "decision_witness"]
    pending = [c for c in checks if c.get("check") == "suggestion_pending"]
    n = meta["n_suggestions"]
    assert len(witnesses) >= 0.95 * n, (
        f"only {len(witnesses)}/{n} cards have an algebraic witness"
    )
    assert pending, "no card is left pending, so the task never tests restraint"
    assert len(expected["resolved"]) + len(pending) >= 0.95 * n


# ---------------------------------------------------------------------------
# the algebra, checked independently of the model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", STRESS_SPECS, ids=lambda s: s["scenario_id"])
def test_l5_projection_matches_the_model(spec):
    """SPEC L5 and mockdocs must agree about the end state (L1/L3/L7)."""
    directory = STRESS_ROOT / spec["scenario_id"]
    seed = _json(directory / "seed.json")
    expected = _json(directory / "expected.json")
    _backend, doc = seeded_backend(seed)
    _doc, decisions, _task = decisions_for(spec)
    fresh = doc.clone()
    _apply(fresh, decisions)
    assert project(doc, decisions) == fresh.display_text()
    assert fresh.display_text() == expected["final_text"]


@pytest.mark.parametrize("spec", STRESS_SPECS, ids=lambda s: s["scenario_id"])
def test_every_witness_is_falsified_by_the_wrong_call(spec):
    """A witness that survives the opposite decision would grade nothing."""
    directory = STRESS_ROOT / spec["scenario_id"]
    seed = _json(directory / "seed.json")
    _backend, doc = seeded_backend(seed)
    _doc, decisions, _task = decisions_for(spec)
    for sid in sorted(doc.registry)[:25]:
        text = witness(doc, decisions, sid)
        if text is None:
            continue
        assert text in project(doc, decisions)
        for other in ("accept", "reject", None):
            if other == decisions.get(sid):
                continue
            alternative = dict(decisions)
            if other is None:
                alternative.pop(sid, None)
            else:
                alternative[sid] = other
            alternative_text = project(doc, alternative)
            if alternative_text == project(doc, decisions):
                continue
            assert text not in alternative_text, (
                f"{sid}: witness survives the {other or 'pending'} call"
            )


# ---------------------------------------------------------------------------
# the material is real
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("document", prose.DOCUMENTS, ids=lambda d: d.key)
def test_base_documents_are_article_shaped_prose(document):
    document.validate()
    assert 1500 <= document.word_count <= 4000
    assert len(document.headings) >= 6
    sentence_spans = all_sentences(document.text)
    assert len(sentence_spans) >= 60
    lengths = [end - start for start, end in sentence_spans]
    average = sum(lengths) / len(lengths)
    assert 60 <= average <= 220, f"average sentence length {average:.0f} is not prose"
    # Filler would repeat; real writing does not.
    tokens = [w.lower() for w in document.text.split()]
    assert len(set(tokens)) / len(tokens) > 0.35, "vocabulary is too repetitive"


@pytest.mark.parametrize("document", prose.DOCUMENTS, ids=lambda d: d.key)
def test_every_edit_lands_on_a_linguistic_boundary(document):
    """No candidate may start or end in the middle of a word."""
    text = document.text
    for candidate in candidates(document):
        for index in (candidate.start, candidate.end):
            if 0 < index < len(text):
                left, right = text[index - 1], text[index]
                assert not (left.isalpha() and right.isalpha()), (
                    f"{candidate.kind} splits a word at {index}: "
                    f"{text[index - 12 : index + 12]!r}"
                )


@pytest.mark.parametrize("spec", STRESS_SPECS, ids=lambda s: s["scenario_id"])
def test_the_edit_mix_is_realistic(spec):
    """Many small copyedits, a few large rewrites, from several reviewers."""
    from llmux.scenarios.stressgen.catalog import walk_for

    walk = walk_for(spec)
    sizes = walk.size_histogram()
    small = sizes["tiny (<=12 chars)"] + sizes["small (13-40)"]
    large = sizes["large (>100)"]
    total = sum(sizes.values())
    assert small / total > 0.5, f"not enough small copyedits: {dict(sizes)}"
    assert large / total < 0.2, f"too many large rewrites: {dict(sizes)}"
    assert len(walk.kind_histogram()) >= 8, "the edit mix is too monotonous"
    reviewers = walk.reviewer_histogram()
    assert len(reviewers) == 5
    assert max(reviewers.values()) / total < 0.55, "one reviewer dominates"
    kinds = Counter(a.kind for a in walk.applied)
    assert set(kinds) <= {k.name for k in KINDS}
