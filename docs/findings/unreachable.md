# Findings: the items that are unreachable by construction

Resolved by the orchestrator without API access, because the question is
answered by the shape of the API surface rather than by its behaviour. Each
one had been carried in HANDOVER §7 as "untested"; the honest status is
**not reachable by any tool under test**, which is a stronger and more stable
statement than "we have not got round to it".

## 1. `runColour` / cross-author colour precedence (mock spec §13.1)

**Verdict: RESOLVED — unobservable, permanently.**

`mockdocs/model.py:193` tracks "author of the most recently applied mark" to
back `runColour`, and the spec flags cross-author precedence as unresolved.

Evidence that no divergence here can ever be observed by a tool:

- `grep -in "colou?r" docs/preview-api-reference.md` → **no match**. Colour
  does not appear anywhere in the transcription of the API.
- `grep -rin "colou?r"` across `e2e/*.py` and `tests/gdocs_preview/*.py` →
  **no match**. No captured payload, fixture, or assertion mentions it.
- `gdocs_preview/` contains no colour handling at all.

The Docs `Document` JSON carries `suggestedInsertionIds` /
`suggestedDeletionIds` per element; the *colour* a suggestion is rendered in
is an editor concern computed client-side from authorship, and is never
serialised. So `runColour`'s precedence rule is an internal detail of the
mock, exercised only by invariant I3, and cannot change any answer any MCP
tool gives.

**Consequence:** this should stop being listed as an open question about the
API. It is a closed question about the mock's internals.

## 2. §5.4 backspace-burst destructive deletion, and §9 undo

**Verdict: RESOLVED — outside the API surface, permanently.**

Both are editor-interaction semantics: what Docs does when a human holds
backspace inside an active typing burst, and what undo restores. The only
mutation channel this repo has is `documents.batchUpdate`, whose request
types are declarative edits (`insertText`, `deleteContentRange`,
`acceptSuggestion`, …). There is no "burst", no keystroke timing, and no undo
request type.

The mock spec already reached this conclusion (§"Skipped sections", lines
403-405: "no tool under test can reach them"). This confirms it from the
other direction: not merely that the mock skips them, but that no test could
distinguish an implementation from a non-implementation through the API.

**Consequence:** §13.4 is untestable-by-construction, not untested.

## 3. Enrollment: per-project or per-account? (open UNCERTAIN item 3)

**Verdict: BLOCKED — and the blocker is external, not effort.**

Deciding this requires a **second GCP project that is not enrolled in the
Developer Preview**, with its own OAuth client and a consent grant. Creating
one requires a human in a browser: project creation, API enablement, consent
screen configuration, and an interactive OAuth approval. None of that is
reachable from an autonomous run.

What can be said without it is already said correctly in HANDOVER §2.1: the
repo *treats* enrollment as a property of the GCP project, the error message
says so, and the caveat is recorded. That is the honest position and it does
not need softening — it needs a second project.

See `docs/findings/errors-and-discovery.md` for the separate, and answerable,
question of whether the *classifier's marker strings* match what the live API
really emits for a proto-parse failure. That one was resolved by provoking
real unknown-field errors from an enrolled project, which exercises the same
code path in the API.
