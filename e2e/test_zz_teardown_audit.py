"""Finalizer test: proves scratch-doc teardown actually ran.

Named test_zz_* so it executes after every other e2e module (pytest
orders by path). Every doc registered by earlier tests must already be
cleaned by its fixture teardown - and we re-verify against Drive itself,
not just the tracker's bookkeeping.
"""

from __future__ import annotations

import pytest
from googleapiclient.errors import HttpError

pytestmark = pytest.mark.e2e_ga


def test_all_scratch_docs_cleaned(doc_tracker, harness_drive):
    uncleaned = [e for e in doc_tracker.entries if not e["cleaned"]]
    assert not uncleaned, (
        "scratch docs left uncleaned by fixture teardown: "
        + ", ".join(f"{e['doc_id']} ({e['title']})" for e in uncleaned)
    )

    for entry in doc_tracker.entries:
        doc_id = entry["doc_id"]
        try:
            meta = (
                harness_drive.files().get(fileId=doc_id, fields="id, trashed").execute()
            )
        except HttpError as error:
            status = getattr(getattr(error, "resp", None), "status", None)
            assert status == 404, f"unexpected error auditing {doc_id}: {error}"
            continue  # permanently deleted - clean
        assert meta.get("trashed") is True, (
            f"scratch doc {doc_id} ({entry['title']}) survived teardown "
            f"untrashed (method recorded: {entry['method']})"
        )
