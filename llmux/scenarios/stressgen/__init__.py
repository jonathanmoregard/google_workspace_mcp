"""Monte-Carlo stress corpus: large, realistic review sets.

The hand-written corpus in ``llmux/scenarios/generated`` saturated -- 32 of
32 agent runs passed it, so it no longer discriminates between models. This
package builds the benchmark that does: the same tool surface and the same
algebra, applied to review sets of 30, 60, 90 and 120 pending suggestions on
genuine 1,500-1,800 word articles.

Layout::

    prose.py       four hand-written base documents        (the material)
    edits.py       linguistic spans + editorial edit kinds (where and what)
    walk.py        the seeded random walk over the algebra (the review)
    invariants.py  L5 projection and per-card witnesses    (the judge)
    catalog.py     the four scenarios and their rule lists (the tasks)
    build.py       write the corpus into ../stress/        (the generator)
    measure.py     context cost and score calibration      (the instrument)

Everything is deterministic from fixed seeds, so the corpus lives in git and
is regenerated as a test.
"""
