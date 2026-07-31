"""Write the stress corpus.

    uv run python -m llmux.scenarios.stressgen.build
    uv run python -m llmux.scenarios.stressgen.build --only stress-030-faq-copyedit
    uv run python -m llmux.scenarios.stressgen.build --search-seeds

The heavy lifting is :func:`llmux.scenarios.generate.build`, unchanged: same
contract files, same model-path-versus-tool-path cross-check, same refusal
to write a scenario whose own oracle does not grade 1.0. This module only
supplies a different catalogue and a different output directory, so the
stress corpus cannot drift away from the corpus contract the runner and the
graders already speak.

``--search-seeds`` exists because a stress scenario can fail to be *fair*
rather than fail to build -- a card the task says to leave pending that some
other decision destroys. The search walks seeds upward until one produces a
fair scenario and prints it, so the fix is a constant in
:mod:`llmux.scenarios.stressgen.catalog` rather than a retry at build time.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Optional

from llmux.scenarios import generate as gen
from llmux.scenarios.catalog import Scenario
from llmux.scenarios.primitives import seeded_backend
from llmux.scenarios.stressgen.catalog import (
    STRESS_SPECS,
    StressBuildError,
    build_stress_scenario,
)

STRESS_ROOT = Path(__file__).resolve().parents[1] / "stress"


def scenarios(only: Optional[list[str]] = None) -> list[Scenario]:
    return [
        build_stress_scenario(**spec)
        for spec in STRESS_SPECS
        if not only or spec["scenario_id"] in only
    ]


def generate(only: Optional[list[str]] = None, root: Path = STRESS_ROOT) -> list[str]:
    """Build every stress scenario and write its directory. Returns the ids."""
    root.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for scenario in scenarios(only):
        _backend, doc = seeded_backend(scenario.seed)
        problems = gen.check_brief(scenario, sorted(doc.registry))
        if problems:
            raise gen.GenerationError(f"{scenario.id}: " + "; ".join(problems))
        files = gen.build(scenario)
        gen.write(scenario, files, root)
        written.append(scenario.id)
    return written


def search_seeds(limit: int = 40) -> dict[str, Any]:
    """First fair seed for each spec, walking upward from 1."""
    found: dict[str, Any] = {}
    for spec in STRESS_SPECS:
        for candidate in range(1, limit + 1):
            trial = dict(spec, seed=candidate)
            try:
                build_stress_scenario(**trial)
            except (StressBuildError, RuntimeError):
                continue
            found[spec["scenario_id"]] = candidate
            break
        else:
            found[spec["scenario_id"]] = None
    return found


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", help="scenario ids to rebuild")
    parser.add_argument("--root", default=str(STRESS_ROOT), help="output directory")
    parser.add_argument(
        "--search-seeds",
        action="store_true",
        help="report the first seed that yields a fair scenario for each spec",
    )
    args = parser.parse_args(argv)

    if args.search_seeds:
        for scenario_id, seed in search_seeds().items():
            print(f"{scenario_id}: seed={seed}")
        return 0

    root = Path(args.root)
    try:
        written = generate(only=args.only, root=root)
    except (gen.GenerationError, StressBuildError) as exc:
        print(f"stress generation failed: {exc}", file=sys.stderr)
        return 1
    for scenario_id in written:
        print(f"wrote {root / scenario_id}")
    print(f"{len(written)} stress scenario(s)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
