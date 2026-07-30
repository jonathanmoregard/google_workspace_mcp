"""Loading the frozen scenario contract and calling a scenario's grader.

The runner consumes ``llmux/scenarios/generated/`` without owning it, so a
malformed scenario has to fail loudly at load time -- before a batch spends
tokens on it -- rather than halfway through.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llmux.runner import scenarios as scen

FIXTURES = Path(__file__).resolve().parents[2] / "llmux" / "runner" / "_fixtures"


def write_scenario(root: Path, scenario_id: str, **overrides) -> Path:
    path = root / scenario_id
    path.mkdir(parents=True, exist_ok=True)
    files = {
        "seed.json": json.dumps({"documents": [{"document_id": "d", "text": "hi\n"}]}),
        "brief.md": "Do the thing.",
        "expected.json": json.dumps({"final_text": "hi\n"}),
        "grade.py": (
            "def grade(backend):\n"
            "    return {'pass': True, 'score': 1.0, 'failures': []}\n"
        ),
        "meta.json": json.dumps(
            {
                "id": scenario_id,
                "difficulty": "easy",
                "steps": 1,
                "tags": ["t"],
                "authors": [],
                "n_suggestions": 0,
            }
        ),
    }
    files.update(overrides)
    for name, content in files.items():
        if content is None:
            continue
        (path / name).write_text(content, encoding="utf-8")
    return path


def test_fixture_corpus_satisfies_the_contract():
    found = scen.discover(FIXTURES)
    assert {s.id for s in found} == {
        "fx-accept-reject",
        "fx-anchored-comment",
        "fx-suggest-utf16",
    }
    for scenario in found:
        assert scenario.brief
        assert scenario.seed_path.is_file()
        assert scenario.difficulty in scen.DIFFICULTY_ORDER
        assert scenario.meta["n_suggestions"] is not None


def test_missing_contract_file_is_rejected(tmp_path):
    write_scenario(tmp_path, "broken", **{"grade.py": None})
    assert not (tmp_path / "broken" / "grade.py").exists()
    with pytest.raises(scen.ScenarioContractError, match="grade.py"):
        scen.load_scenario(tmp_path / "broken")


def test_meta_id_must_match_the_directory_name(tmp_path):
    write_scenario(tmp_path, "dir-name", **{"meta.json": json.dumps({"id": "other"})})
    with pytest.raises(scen.ScenarioContractError, match="does not match"):
        scen.load_scenario(tmp_path / "dir-name")


def test_empty_brief_is_rejected(tmp_path):
    write_scenario(tmp_path, "empty-brief", **{"brief.md": "   \n"})
    with pytest.raises(scen.ScenarioContractError, match="brief.md is empty"):
        scen.load_scenario(tmp_path / "empty-brief")


def test_missing_corpus_names_the_fixtures_as_the_way_out(tmp_path):
    with pytest.raises(scen.ScenarioContractError, match="_fixtures"):
        scen.discover(tmp_path / "nope")


def test_difficulty_comes_from_tier_then_from_the_numeric_rank(tmp_path):
    """The corpus states difficulty twice: a ``tier`` name and a 1-5 rank."""
    both = scen.load_scenario(
        write_scenario(
            tmp_path,
            "both",
            **{"meta.json": json.dumps({"id": "both", "difficulty": 5, "tier": "adversarial"})},
        )
    )
    assert both.difficulty == "adversarial"
    assert both.rank == 5

    rank_only = scen.load_scenario(
        write_scenario(
            tmp_path, "rank", **{"meta.json": json.dumps({"id": "rank", "difficulty": 4})}
        )
    )
    assert rank_only.difficulty == "hard"
    assert rank_only.rank == 4

    neither = scen.load_scenario(
        write_scenario(tmp_path, "none", **{"meta.json": json.dumps({"id": "none"})})
    )
    assert neither.difficulty == "unknown"


def test_identity_prefers_the_seed_then_meta(tmp_path):
    from_seed = scen.load_scenario(
        write_scenario(
            tmp_path,
            "seeded-me",
            **{
                "seed.json": json.dumps({"me": "reviewer", "documents": []}),
                "meta.json": json.dumps({"id": "seeded-me", "me": "someone-else"}),
            },
        )
    )
    assert from_seed.me == "reviewer"

    from_meta = scen.load_scenario(
        write_scenario(
            tmp_path,
            "meta-me",
            **{
                "seed.json": json.dumps({"documents": []}),
                "meta.json": json.dumps({"id": "meta-me", "me": "reviewer"}),
            },
        )
    )
    assert from_meta.me == "reviewer"


def test_scenario_env_passes_mock_flags_but_never_the_runner_paths(tmp_path):
    scenario = scen.load_scenario(
        write_scenario(
            tmp_path,
            "flagged",
            **{
                "meta.json": json.dumps(
                    {
                        "id": "flagged",
                        "env": {
                            "MOCKDOCS_SEED": "seed.json",
                            "MOCKDOCS_STATE_DUMP": "/tmp/theirs.json",
                            "MOCKDOCS_FAIL_COMMENTS": "1",
                        },
                    }
                )
            },
        )
    )
    assert scenario.server_env == {"MOCKDOCS_FAIL_COMMENTS": "1"}


def test_discover_filters_and_limits(tmp_path):
    write_scenario(tmp_path, "a-easy")
    write_scenario(
        tmp_path,
        "b-hard",
        **{"meta.json": json.dumps({"id": "b-hard", "difficulty": "hard"})},
    )
    write_scenario(tmp_path, "c-easy")
    write_scenario(tmp_path, "_private")

    everything = scen.discover(tmp_path)
    assert [s.id for s in everything] == ["a-easy", "c-easy", "b-hard"], (
        "cheapest difficulty first, so --limit truncates the cheap end"
    )
    assert [s.id for s in scen.discover(tmp_path, limit=2)] == ["a-easy", "c-easy"]
    assert [s.id for s in scen.discover(tmp_path, ids=["b-hard"])] == ["b-hard"]
    assert [s.id for s in scen.discover(tmp_path, difficulties=["hard"])] == ["b-hard"]

    with pytest.raises(scen.ScenarioContractError, match="unknown scenario id"):
        scen.discover(tmp_path, ids=["ghost"])


def test_grade_result_shape_is_validated(tmp_path):
    scenario = scen.load_scenario(write_scenario(tmp_path, "ok"))
    result = scen.grade_backend(scenario, object())
    assert result.passed is True
    assert result.score == 1.0
    assert result.error is None

    bad = scen.load_scenario(
        write_scenario(tmp_path, "bad", **{"grade.py": "def grade(b):\n    return 42\n"})
    )
    crashed = scen.grade_backend(bad, object())
    assert crashed.passed is False
    assert "must return a dict" in (crashed.error or "")

    missing_keys = scen.load_scenario(
        write_scenario(
            tmp_path, "thin", **{"grade.py": "def grade(b):\n    return {'pass': True}\n"}
        )
    )
    assert "score" in (scen.grade_backend(missing_keys, object()).error or "")


def test_a_crashing_grader_fails_the_run_instead_of_the_batch(tmp_path):
    scenario = scen.load_scenario(
        write_scenario(
            tmp_path, "boom", **{"grade.py": "def grade(b):\n    raise ValueError('x')\n"}
        )
    )
    result = scen.grade_backend(scenario, object())
    assert result.passed is False
    assert "ValueError" in (result.error or "")


def test_grade_normalisation_accepts_a_single_failure_string():
    result = scen.normalise_grade({"pass": False, "score": 0.5, "failures": "one thing"})
    assert result.failures == ("one thing",)


def test_fixture_graders_pass_on_their_solved_end_state():
    """The fixtures have to be winnable, or the harness measures nothing."""
    from mockdocs.fake_services import FakeBackend

    accept_reject = scen.load_scenario(FIXTURES / "fx-accept-reject")
    backend = FakeBackend()
    backend.seed(accept_reject.seed)
    doc = backend.documents["fx-doc-typo"]
    doc.accept("sug.bob.2")
    doc.reject("sug.carol.1")
    assert scen.grade_backend(accept_reject, backend).passed is True

    utf16 = scen.load_scenario(FIXTURES / "fx-suggest-utf16")
    backend = FakeBackend()
    backend.seed(utf16.seed)
    backend.documents["fx-doc-utf16"].replace(21, 27, "color", backend.me)
    assert scen.grade_backend(utf16, backend).passed is True

    comment = scen.load_scenario(FIXTURES / "fx-anchored-comment")
    backend = FakeBackend()
    backend.seed(comment.seed)
    backend.create_comment_thread("fx-doc-comment", "Source?", quote="40%")
    assert scen.grade_backend(comment, backend).passed is True


def test_fixture_graders_fail_on_the_untouched_seed():
    from mockdocs.fake_services import FakeBackend

    for scenario in scen.discover(FIXTURES):
        backend = FakeBackend()
        backend.seed(scenario.seed)
        result = scen.grade_backend(scenario, backend)
        assert result.passed is False, f"{scenario.id} passes without doing anything"
        assert result.failures
