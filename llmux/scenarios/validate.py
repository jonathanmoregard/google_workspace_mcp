"""Prove every scenario is solvable and graded correctly.

    uv run python -m llmux.scenarios.validate

For each scenario directory this replays ``solution.json`` through the real
MCP tools against a backend seeded from ``seed.json``, loads that scenario's
own ``grade.py``, and requires ``pass=True`` with ``score=1.0``. An
unsolvable or mis-graded scenario would turn every LLM run against it into
noise -- the model would be measured against an end state nothing can reach,
or credited for one it never had to produce -- so this is a gate, not a
diagnostic.

Two further checks, both cheap and both aimed at ways a self-generated
corpus goes quietly wrong:

- **the do-nothing run must fail.** A grader that passes an untouched
  document is not grading anything. Every scenario is graded a second time
  against its own seed with no steps applied, and that verdict must be a
  failure with a score below 1.
- **the near-miss run must fail.** A scenario that ships a
  ``naive_solution.json`` -- its own solution with every ``segment_id``
  dropped, which is what an agent produces when it takes a header card's
  ``start_index`` and hands it back bare -- is graded a third time against
  that, and it must NOT pass. A trap nobody can fall into is decoration:
  this is what makes "the correct solution requires addressing a non-body
  segment" a checked claim rather than a docstring.
- **the brief must not leak.** No suggestion id, no comment id; the document
  id must be present. A brief that names ids removes the discovery step that
  is most of what we are trying to measure.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from mockdocs.fake_services import FakeBackend

from llmux.scenarios.oracle import run_solution

GENERATED = Path(__file__).resolve().parent / "generated"

REQUIRED_FILES = ("seed.json", "brief.md", "expected.json", "grade.py", "meta.json")
EXPECTED_KEYS = (
    "final_text",
    "surviving_suggestion_ids",
    "resolved",
    "thread_expectations",
    "invariant_checks",
)


@dataclass
class Result:
    scenario_id: str
    ok: bool = True
    score: float = 0.0
    problems: list[str] = field(default_factory=list)

    def fail(self, message: str) -> "Result":
        self.ok = False
        self.problems.append(message)
        return self


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_grader(scenario_dir: Path) -> Callable[[Any], dict[str, Any]]:
    """Import ``<scenario>/grade.py`` under a unique module name."""
    path = scenario_dir / "grade.py"
    name = f"llmux_scenario_grade_{scenario_dir.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - unreadable file
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    grade = getattr(module, "grade", None)
    if not callable(grade):
        raise ImportError(f"{path} does not expose a callable grade(backend)")
    return grade


def backend_from_seed(seed: dict[str, Any]) -> FakeBackend:
    backend = FakeBackend()
    backend.seed(seed)
    return backend


def validate_scenario(scenario_dir: Path) -> Result:
    result = Result(scenario_id=scenario_dir.name)

    for name in REQUIRED_FILES:
        if not (scenario_dir / name).exists():
            return result.fail(f"missing {name}")

    seed = _read_json(scenario_dir / "seed.json")
    expected = _read_json(scenario_dir / "expected.json")
    meta = _read_json(scenario_dir / "meta.json")
    brief = (scenario_dir / "brief.md").read_text(encoding="utf-8")
    solution_path = scenario_dir / "solution.json"
    if not solution_path.exists():
        return result.fail("missing solution.json (the oracle cannot be replayed)")
    solution = _read_json(solution_path)

    document_id = expected.get("document_id")
    if not document_id:
        return result.fail("expected.json has no document_id")
    for key in EXPECTED_KEYS:
        if key not in expected:
            result.fail(f"expected.json is missing the contract key {key!r}")

    # -- contract checks on the metadata -----------------------------------
    if meta.get("id") != scenario_dir.name:
        result.fail(f"meta.id {meta.get('id')!r} != directory {scenario_dir.name!r}")
    if not isinstance(meta.get("difficulty"), int) or not 1 <= meta["difficulty"] <= 5:
        result.fail(f"meta.difficulty {meta.get('difficulty')!r} is not 1-5")
    if meta.get("steps") != len(solution):
        result.fail(
            f"meta.steps {meta.get('steps')!r} != {len(solution)} solution steps"
        )

    # -- the brief must not do the agent's discovery for it ----------------
    if "sug." in brief:
        result.fail("brief.md names a suggestion id")
    if "comment." in brief:
        result.fail("brief.md names a comment id")
    if document_id not in brief:
        result.fail("brief.md does not state the document id")

    # -- the oracle solves it ----------------------------------------------
    grade = load_grader(scenario_dir)
    backend = backend_from_seed(seed)
    try:
        run_solution(backend, document_id, solution)
    except Exception as exc:
        return result.fail(f"oracle solution did not run: {exc}")

    verdict = grade(backend)
    result.score = float(verdict.get("score", 0.0))
    if not verdict.get("pass"):
        result.fail(f"oracle solution does not pass: {verdict.get('failures')}")
    if result.score != 1.0:
        result.fail(f"oracle solution scored {result.score}, expected 1.0")

    # -- doing nothing must not pass ---------------------------------------
    untouched = grade(backend_from_seed(seed))
    if untouched.get("pass"):
        result.fail("the untouched document passes: the grader checks nothing")
    if float(untouched.get("score", 0.0)) >= 1.0:
        result.fail("the untouched document scores 1.0")

    # -- and neither must the bare-index near-miss --------------------------
    naive_path = scenario_dir / "naive_solution.json"
    if naive_path.exists():
        naive_backend = backend_from_seed(seed)
        try:
            run_solution(naive_backend, document_id, _read_json(naive_path))
        except Exception as exc:
            # The real bug is SILENT -- the wrong write succeeds. A naive
            # run that errors is a different, much friendlier failure, and
            # a scenario claiming to reproduce this one must not rely on it.
            return result.fail(
                f"the bare-index solution errored instead of landing in the "
                f"wrong place: {exc}"
            )
        naive_verdict = grade(naive_backend)
        if naive_verdict.get("pass"):
            result.fail(
                "the bare-index solution PASSES: dropping every segment_id "
                "changes nothing, so this scenario does not require an address"
            )
        if float(naive_verdict.get("score", 0.0)) >= 1.0:
            result.fail("the bare-index solution scores 1.0")

    return result


def validate_all(root: Path = GENERATED) -> list[Result]:
    directories = sorted(p for p in root.iterdir() if (p / "meta.json").exists())
    return [validate_scenario(p) for p in directories]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(GENERATED))
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.exists():
        print(f"no corpus at {root}", file=sys.stderr)
        return 1
    results = validate_all(root)
    if not results:
        print(f"no scenarios under {root}", file=sys.stderr)
        return 1

    width = max(len(r.scenario_id) for r in results)
    for r in results:
        status = "ok  " if r.ok else "FAIL"
        print(f"{status} {r.scenario_id:<{width}}  score={r.score:.2f}")
        for problem in r.problems:
            print(f"       - {problem}")
    failed = [r for r in results if not r.ok]
    print(f"{len(results) - len(failed)}/{len(results)} scenarios valid")
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
