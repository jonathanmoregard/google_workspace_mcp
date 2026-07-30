"""Scenario generator for the LLM-UX harness.

A scenario is a convoluted, multi-step document-review task whose ground
truth is *computed*, never authored: the seed is a sequence of SPEC §5 edit
operations, the task is a predicate over the resulting suggestion registry,
and the expected end state is whatever :mod:`mockdocs.model` produces when
the intended solution is applied. Nothing in ``expected.json`` encodes a
belief about §7 -- it encodes §7.

Layout::

    primitives.py  locators, moves, predicates, SeedBuilder   (the algebra)
    steps.py       oracle steps: one MCP tool call each        (the solution)
    catalog.py     the scenario definitions                    (the corpus)
    generate.py    build + cross-check + write                 (the generator)
    grading.py     end-state grading shared by every grade.py  (the judge)
    oracle.py      replay a solution through the real tools    (the prover)
    validate.py    every scenario is solvable and graded right (the gate)
    generated/     the committed corpus
    stressgen/     the Monte-Carlo stress corpus generator
    stress/        the committed stress corpus (30/60/90/120 suggestions)

``stressgen`` reuses all of the above -- same primitives, same steps, same
``generate.build`` cross-check, same ``validate`` gate -- and adds three
things the hand-written ladder does not need: real 1,500-1,800 word
articles to review, a seeded random walk that samples realistic editorial
edits at linguistically meaningful positions, and per-suggestion grading
derived from SPEC L5 so a 120-card task scores as a curve rather than a
cliff.
"""
