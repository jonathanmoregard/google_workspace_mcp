"""Scenarios where a second human edits the document while the agent reviews.

A corpus, not a package of code: each subdirectory satisfies the same frozen
contract as ``llmux/scenarios/generated`` (``seed.json``, ``brief.md``,
``expected.json``, ``grade.py``, ``meta.json``), so the runner consumes it
unchanged:

    uv run python -m llmux.runner.run --corpus llmux/interference --all

What is different is one optional ``meta.json`` key, ``interferences``: a
script of operations the *other* editor performs, each pinned to a point in
the agent's own call sequence (see :mod:`mockdocs.concurrency`). Nothing else
about the contract moves, which is deliberate -- an interference scenario has
to be gradeable and comparable next to a single-writer one.

``grading.py`` here is shared helper code, not a scenario; the runner's
discovery only walks directories.
"""
