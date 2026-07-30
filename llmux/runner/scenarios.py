"""Load the scenario corpus and run a scenario's grader.

The corpus contract is frozen (``llmux/scenarios/generated/<id>/``):

======================  ====================================================
``seed.json``           mockdocs seed state (``FakeBackend.seed``)
``brief.md``            the natural-language task handed to the agent
``expected.json``       ground truth (documentation for humans and graders)
``grade.py``            module exposing ``grade(backend) -> {...}``
``meta.json``           ``{id, difficulty, steps, tags, authors, n_suggestions}``
======================  ====================================================

Everything here validates that contract loudly: a malformed scenario should
fail before any tokens are spent, not halfway through a batch.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

REQUIRED_FILES = ("seed.json", "brief.md", "expected.json", "grade.py", "meta.json")

#: Ordering used whenever difficulty needs to be sorted or bucketed.
#: ``--limit`` truncates from the right, so cheap buckets come first.
DIFFICULTY_ORDER = ("easy", "medium", "hard", "adversarial", "unknown")

#: The corpus carries difficulty two ways: a ``tier`` string and a 1-5
#: ``difficulty`` number. ``tier`` wins when present; the number is the
#: fallback so a scenario that only has one still lands in a bucket.
DIFFICULTY_BY_RANK = {1: "easy", 2: "easy", 3: "medium", 4: "hard", 5: "adversarial"}


class ScenarioContractError(Exception):
    """A scenario directory does not satisfy the frozen contract."""


@dataclass(frozen=True)
class Scenario:
    """One loaded scenario directory."""

    id: str
    path: Path
    brief: str
    seed: dict[str, Any]
    expected: dict[str, Any]
    meta: dict[str, Any]

    @property
    def seed_path(self) -> Path:
        return self.path / "seed.json"

    @property
    def grade_path(self) -> Path:
        return self.path / "grade.py"

    @property
    def difficulty(self) -> str:
        """Bucket name: ``tier`` if the corpus states one, else the rank."""
        tier = str(self.meta.get("tier") or "").lower()
        if tier in DIFFICULTY_ORDER:
            return tier
        raw = self.meta.get("difficulty")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return DIFFICULTY_BY_RANK.get(int(raw), "unknown")
        value = str(raw or "unknown").lower()
        return value if value in DIFFICULTY_ORDER else "unknown"

    @property
    def rank(self) -> int:
        """Numeric difficulty when the corpus gives one, else the bucket's."""
        raw = self.meta.get("difficulty")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return int(raw)
        return DIFFICULTY_ORDER.index(self.difficulty) + 1

    @property
    def steps(self) -> int:
        try:
            return int(self.meta.get("steps") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def tags(self) -> tuple[str, ...]:
        raw = self.meta.get("tags") or []
        return tuple(str(t).lower() for t in raw)

    @property
    def me(self) -> str:
        """Authenticated user for the run (the seed owns this)."""
        return str(self.seed.get("me") or self.meta.get("me") or "mockuser")

    @property
    def server_env(self) -> dict[str, str]:
        """``meta.env`` entries the runner is allowed to pass through.

        A scenario can ask for mock behaviour it needs -- ``MOCKDOCS_NOT_ENROLLED``,
        ``MOCKDOCS_FAIL_COMMENTS`` -- but never for the seed or state-dump
        paths: those are the runner's, and a relative ``seed.json`` from the
        corpus would resolve against the agent's scratch cwd.
        """
        reserved = {"MOCKDOCS_SEED", "MOCKDOCS_STATE_DUMP"}
        env = self.meta.get("env") or {}
        if not isinstance(env, dict):
            return {}
        return {
            str(k): str(v) for k, v in env.items() if str(k) not in reserved
        }


@dataclass(frozen=True)
class GradeResult:
    """Normalised output of a scenario's ``grade(backend)``."""

    passed: bool
    score: float
    failures: tuple[str, ...] = ()
    error: Optional[str] = None

    @classmethod
    def crashed(cls, message: str) -> "GradeResult":
        return cls(passed=False, score=0.0, failures=(message,), error=message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pass": self.passed,
            "score": self.score,
            "failures": list(self.failures),
            "error": self.error,
        }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ScenarioContractError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ScenarioContractError(f"{path} must contain a JSON object")
    return data


def load_scenario(path: Path) -> Scenario:
    """Load and validate one scenario directory."""
    path = Path(path)
    missing = [name for name in REQUIRED_FILES if not (path / name).is_file()]
    if missing:
        raise ScenarioContractError(
            f"scenario {path.name} is missing {', '.join(missing)} (contract: "
            f"{', '.join(REQUIRED_FILES)})"
        )
    meta = _read_json(path / "meta.json")
    scenario_id = str(meta.get("id") or path.name)
    if scenario_id != path.name:
        raise ScenarioContractError(
            f"scenario id {scenario_id!r} in meta.json does not match its "
            f"directory name {path.name!r}"
        )
    brief = (path / "brief.md").read_text(encoding="utf-8").strip()
    if not brief:
        raise ScenarioContractError(f"scenario {scenario_id}: brief.md is empty")
    return Scenario(
        id=scenario_id,
        path=path,
        brief=brief,
        seed=_read_json(path / "seed.json"),
        expected=_read_json(path / "expected.json"),
        meta=meta,
    )


def discover(
    corpus: Path,
    *,
    ids: Optional[Iterable[str]] = None,
    difficulties: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
) -> list[Scenario]:
    """Load every scenario under ``corpus``, filtered and capped.

    Sorted by (difficulty, id) so ``--limit`` yields the cheapest scenarios
    first and a truncated batch stays reproducible.
    """
    corpus = Path(corpus)
    if not corpus.is_dir():
        raise ScenarioContractError(
            f"no scenario corpus at {corpus}. Point --corpus at M2's "
            f"llmux/scenarios/generated, or at llmux/runner/_fixtures to "
            f"exercise the harness."
        )
    wanted = {str(i) for i in ids} if ids else None
    wanted_difficulty = {str(d).lower() for d in difficulties} if difficulties else None

    found: list[Scenario] = []
    for child in sorted(p for p in corpus.iterdir() if p.is_dir()):
        if child.name.startswith((".", "_")):
            continue
        scenario = load_scenario(child)
        if wanted is not None and scenario.id not in wanted:
            continue
        if wanted_difficulty is not None and scenario.difficulty not in wanted_difficulty:
            continue
        found.append(scenario)

    if wanted is not None:
        unknown = wanted - {s.id for s in found}
        if unknown:
            raise ScenarioContractError(
                f"unknown scenario id(s): {', '.join(sorted(unknown))}"
            )
    found.sort(key=lambda s: (DIFFICULTY_ORDER.index(s.difficulty), s.rank, s.id))
    if limit is not None:
        found = found[:limit]
    return found


def load_grader(scenario: Scenario) -> Any:
    """Import ``grade.py`` under a scenario-unique module name."""
    module_name = f"llmux_grade_{scenario.id.replace('-', '_').replace('.', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, scenario.grade_path)
    if spec is None or spec.loader is None:  # pragma: no cover - unreachable
        raise ScenarioContractError(f"cannot import {scenario.grade_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    grade = getattr(module, "grade", None)
    if not callable(grade):
        raise ScenarioContractError(
            f"{scenario.grade_path} does not expose a callable grade(backend)"
        )
    return grade


def normalise_grade(payload: Any) -> GradeResult:
    """Validate a grader's return value against the contract."""
    if not isinstance(payload, dict):
        raise ScenarioContractError(
            f"grade() must return a dict, got {type(payload).__name__}"
        )
    if "pass" not in payload or "score" not in payload:
        raise ScenarioContractError(
            f"grade() must return keys 'pass' and 'score', got {sorted(payload)}"
        )
    failures = payload.get("failures") or []
    if isinstance(failures, str):
        failures = [failures]
    try:
        score = float(payload["score"])
    except (TypeError, ValueError) as exc:
        raise ScenarioContractError(f"grade() score is not a number: {exc}") from exc
    return GradeResult(
        passed=bool(payload["pass"]),
        score=score,
        failures=tuple(str(f) for f in failures),
    )


def grade_backend(scenario: Scenario, backend: Any) -> GradeResult:
    """Grade an end state; a crashing grader is a failed run, not a crash."""
    try:
        grade = load_grader(scenario)
        return normalise_grade(grade(backend))
    except ScenarioContractError as exc:
        return GradeResult.crashed(f"grader contract violation: {exc}")
    except Exception as exc:  # pragma: no cover - defensive
        return GradeResult.crashed(f"grader raised {type(exc).__name__}: {exc}")


@dataclass
class CorpusStats:
    """Counts used by the pre-flight cost estimate."""

    total: int = 0
    by_difficulty: dict[str, int] = field(default_factory=dict)

    @classmethod
    def of(cls, scenarios: Iterable[Scenario]) -> "CorpusStats":
        stats = cls()
        for scenario in scenarios:
            stats.total += 1
            stats.by_difficulty[scenario.difficulty] = (
                stats.by_difficulty.get(scenario.difficulty, 0) + 1
            )
        return stats
