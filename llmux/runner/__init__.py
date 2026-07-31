"""Drive headless Claude agents through review scenarios against the mock.

``run.py`` is the entry point; the rest of the package is the machinery it
composes:

- :mod:`llmux.runner.scenarios` -- load the frozen scenario contract
  (``seed.json`` / ``brief.md`` / ``expected.json`` / ``grade.py`` /
  ``meta.json``) and call a scenario's grader.
- :mod:`llmux.runner.session` -- MCP config + ``claude`` argv for one run.
- :mod:`llmux.runner.transcript` -- parse ``--output-format stream-json``
  into tool calls, turns, cost and usage.
- :mod:`llmux.runner.taxonomy` -- classify what went wrong from the tool-call
  sequence and the grader's failures.
- :mod:`llmux.runner.analyze` -- aggregate runs into the markdown + JSON
  report.
"""
