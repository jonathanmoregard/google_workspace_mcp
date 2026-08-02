# Re-founding `ix-merge-absorb` on a state production can reach — 2026-08-02

`llmux/interference/ix-merge-absorb` was one of five interference scenarios: a
scripted second editor fires mid-run and the agent must adapt. Its premise was
that **two existing same-author suggestions merge into one**, and that the
survivor is the newer of the two — so the id the agent was holding stopped
existing and its reply came back 400.

[`merge.md`](merge.md) settled that against the live API. It never happens.
What the repo had been calling "merge" is **absorption at creation time**: a
SUGGEST edit abutting or overlapping an existing same-author suggestion is
folded into it, **no second id is ever minted**, the *pre-existing* id survives
and comes back under `updatedSummarySuggestionIds` with no
`createdSuggestionIds` at all — and two suggestions that already exist stay two
forever, even when a later edit pushes them into contact or spans both.

So the scenario was scoring agents on a document state the API cannot produce.
Worse than useless: its score read as evidence about real-world behaviour.

## The choice: (b), replace the premise

Three options were on the table.

| | what it costs | why not / why |
|---|---|---|
| **(a) keep it, relabel it truthfully** as mock-only | cheapest, and honest | but it keeps a hard scenario in a five-case corpus that then measures nothing about prod. One fifth of the interference signal spent on a fiction, correctly labelled |
| **(b) replace the premise with the real one** — a new edit absorbed into a pre-existing card | a rewrite of `brief.md`, `meta.json`, `expected.json`, `grade.py` and the oracles | **chosen.** Absorption at creation time is a thing agents will really hit, and it is arguably a *better* adaptation test than the old one: the card your edit lands in is not yours, and its thread is a conversation you did not start |
| **(c) retire it** | leaves four | throwing away a hard case when a prod-real one of the same shape was available |

(b) was picked because it turned out to be genuinely achievable — the test below
is the evidence, not the hope.

## What the scenario is now

The seed is unchanged: `ix-doc-data`, no pending suggestions.

**Just before** the agent's first `suggest_doc_edit` is applied (trigger phase
moved from `after` to `before`), the same account — the phone, the other
browser tab — leaves its own pending suggestion at exactly the anchor the brief
told the agent to write at, and puts a note on its thread:

```json
{"name": "other-device-suggests-the-same-spot", "kind": "overlapping_suggestion",
 "editor": "mockuser", "trigger": {"when": "before", "tool": ["suggest_doc_edit"], "nth": 1},
 "params": {"after_text": "We will release the model weights.", "text": " (pending legal review)"}}
{"name": "other-device-notes-why", "kind": "reply_thread", "editor": "mockuser",
 "trigger": {"when": "before", "tool": ["suggest_doc_edit"], "nth": 1},
 "params": {"suggestion_id": "sug.mockuser.1", "content": "Legal asked for this caveat …"}}
```

The agent's sentence is therefore absorbed. There is no card of its own to
reply on; the thread it has to use already carries someone else's post. The
brief adds the guardrail that makes the trap real rather than academic —
*anything already pending from your other session stays pending* — because on
prod the only way to get a clean card at that spot is to destroy the edit that
is already there.

The `merge_absorb` interference kind is no longer used by any scenario. It
stays in the catalogue (`mockdocs/concurrency.py`, tested directly by
`tests/mockdocs_concurrency/test_catalogue.py`), because it is still a faithful
driver of the mock's own §6 rule — it is simply not a faithful driver of prod's.

## Grading, and why it is survivor-rule-independent

`grade.py` asserts, all of it id-agnostic:

1. exactly **one** pending card (the addition does not get one of its own);
2. `original_text()` unchanged (nothing was applied directly or accepted);
3. `final_text()` = both sentences, replayed rather than hand-written;
4. the card carrying the agent's sentence **also** carries the other session's
   text — a clean card means the other session's edit was destroyed;
5. the other session's note is **still on that thread**;
6. the agent's reply is on it too;
7. the adaptation half: a read after the change, no blind retry.

Nothing anywhere names a suggestion id, and that is load-bearing rather than
stylistic — see the divergence below.

*(Update 2026-08-02: prod's survivor rule has since been measured and is
deterministic — the touched card with the lexicographically greatest
suggestion id absorbs the edit; see `merge.md`'s rewritten sub-finding. The
grading here deliberately stays rule-independent anyway: the rule is
undocumented and id-shaped, the mock's ids follow a different scheme entirely,
and an end state both rules reach is a stronger foundation than either rule.)*

The grader's replay `TIMELINE` also **stopped replaying the agent's reply**.
Only two projections are read out of that replay and a thread post moves
neither, so replaying it bought nothing and cost the one dependency the
scenario is built to avoid: it had to name the surviving id.

### Tested under both merge rules

The claim "this grades an end state prod and the mock both reach" was measured,
not asserted. `mockdocs/model.py`'s `_merge_around` was monkeypatched in a
throwaway script (nothing in the repo changed) to the prod-faithful rule —
survivor = the **pre-existing** card, absorb at most one, no fixpoint — and the
correct run re-graded:

```
# mockdocs §6 as shipped (survivor = newest)
one card: sug.mockuser.2  Add: “We will publish the raw data too. (pending legal review)”
GRADE: pass=True score=1.0

# prod-faithful (survivor = pre-existing)
one card: sug.mockuser.1  Add: “We will publish the raw data too. (pending legal review)”
GRADE: pass=True score=1.0
```

Same card, same label, same thread, same text — a different id, which is the
only thing the two rules disagree about and the only thing not graded. If
`merge.md`'s step-by-step for closing the mock/prod gap is ever carried out,
this scenario is no longer part of the blast radius.

## The honest limit — what the mock cannot stage

Prod's sharpest cue is that the absorbed write **comes back with no
`createdSuggestionIds` at all**. `mockdocs` cannot produce that, under either
survivor rule: §6 mints an id and `adapter._resolve_merges` rewrites the
response to name whichever card survived, so the agent always gets a usable id
back. Measured, in the same probe:

```
mockdocs §6      suggestionResponses: [{"createdSuggestionIds": ["sug.mockuser.2"], …}]
prod-faithful    suggestionResponses: [{"createdSuggestionIds": ["sug.mockuser.1"], …}]
prod (measured)  suggestionResponses: [{"updatedSummarySuggestionIds": ["suggest.e79qrxxlopy"]}]
```

Consequence, stated in `expected.json` and in `grade.py`'s docstring so nobody
reads the score as more than it is: **an agent that simply trusts the id its own
write handed back reaches the right end state here and would have had nothing to
reply to on prod.** Here it is caught only by the re-read half of the adaptation
score, which is a weaker instrument than a 400. That is a mock-fidelity limit,
not a grading bug: the run still fails, it just fails for the process rather
than for the outcome — which is exactly the distinction the corpus was built to
make (`llmux/interference/grading.py`, "credit for adaptation, not for luck").

Closing it properly means making the mock report absorption the way prod does,
which is the `merge.md` project, not an edit.

## Oracles

`tests/mockdocs_concurrency/test_oracles.py`, which is what makes an
interference scenario decidable — a correct run must score 1.0 and a run that
ignores the concurrency must fail:

| oracle | score | why |
|---|---|---|
| correct — write, look again, reply on the card the sentence joined | **1.00 pass** | |
| naive single-writer — replies to the id its own write returned, never looks again | **0.875 FAIL** | right end state, wrong process; on prod there would have been no id |
| clears the way — rejects the shared card to get one of its own | **0.625 FAIL** | loses the other session's text and its note |

The corpus-wide contract still holds: the untouched seed fails as a `HARNESS:`
fault because nothing fired.

## Gate

Run from the branch `fix/merge-absorb-premise` (worktree
`~/worktrees/gdocs-absorb`, branched off `integration/empirics`).

```
uv run python -m llmux.scenarios.validate                                   17/17 scenarios valid
uv run python -m llmux.runner.run --corpus llmux/interference --all --dry-run
                                                                            dry run clean (0 problems)
                                                                            ix-merge-absorb: 2 interference(s)
uv run ruff check .                                                         All checks passed!
uv run ruff format --check .                                                316 files already formatted
uv run pytest tests/mockdocs_concurrency -q                                 65 passed
uv run pytest tests/ -q                                                     2530 passed, 3 skipped
```

The suite baseline was **2529 passed, 3 skipped**; the two `ix-merge-absorb`
oracles were replaced by three (the "clears the way" run is new), so +1 test
and nothing dropped.

**Pre-existing failure, unrelated and not introduced here.**
`tests/llmux_runner/test_run_wiring.py::test_execute_run_grades_classifies_and_stores_artifacts`
fails when `tests/llmux_runner` is run *on its own* — the `fx-anchored-comment`
fixture ends with no comment thread. Reproduced on the untouched
`integration/empirics` worktree at the same commit, identical failure, so it is
an order dependency in that fixture rather than anything from this branch. It
passes in the full-suite run.

No paid llmux batch was run. Ground truth here is computed by the replay and
the oracles, not bought.
