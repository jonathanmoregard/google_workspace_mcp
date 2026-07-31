# Find the suggestions that do nothing

**Document ID:** `mockdoc-a2-noop`
**You are signed in as** `reviewer@example.com`. Anything you suggest, reply
or comment is authored by `reviewer`.

Four rewrites are pending on this one sentence. Some of them replace a piece
of text with **exactly the same text** -- the text they remove and the text
they put in its place are character-for-character identical, so accepting
them would change nothing at all. Those are the ones we want out.

## What to do

For each pending suggestion, compare the text it removes with the text it
adds.

- If the two are identical, then **first** leave a comment anchored to
  exactly the word it replaces, with the content
  `No-op: this suggestion changes nothing.`, and **then** reject that
  suggestion.
- Otherwise -- the two differ, however slightly -- accept it.

Leave nothing pending.

## Ground rules

- Work only through the document review tools. Read the document before you
  change it; suggestion ids are never given to you, you discover them.
- Do exactly what is asked and nothing else: no extra suggestions, comments,
  replies, accepts or rejects.
- When a task says "exactly", it means exactly -- character for character.
