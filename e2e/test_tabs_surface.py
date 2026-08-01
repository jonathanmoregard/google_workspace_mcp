"""Nested tabs and multi-tab thread attribution, against the real API.

Two questions the repo carried as UNVERIFIED until 2026-08-01 (findings:
docs/findings/tabs.md):

1. **Nested tabs.** ``preview_read.tab_documents()`` walks ``childTabs``
   depth-first and had unit coverage only -- no real document with a nested
   tab had ever been read. Nested tabs turn out to be creatable through the
   API (``addDocumentTab`` with ``tabProperties.parentTabId``, which
   upstream's ``manage_doc_tab(action="create", parent_tab_id=...)`` already
   emits), so this needs no hand-made document.
2. **Tab attribution of threads.** The thread-bearing read's top-level
   ``suggestions[]`` and ``comments[]`` carry no tab field of any kind, so
   the repo places a suggestion by the tab whose body carries its id. These
   tests are what says that placement is right about prod rather than about
   a fixture -- and they pin the ONE join a comment thread has, its
   ``anchorId`` against each tab's own ``commentAnchors`` map.

Everything here goes through the MCP surface. Scratch docs come from
``make_scratch_doc`` and are trashed in fixture teardown.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from e2e.mcp_session import tool_json, tool_text
from e2e.run_report import REPORT
from e2e.util import poll_until

#: Deliberately different lengths, so a card read out of the wrong tab is
#: caught by its text and not only by its id.
ROOT_TEXT = "Root tab prose about alpha."
CHILD_TEXT = "Nested child tab prose about gamma, which runs longer than the root."
TOP2_TEXT = "Second top-level tab prose about beta."


@dataclass
class TabbedDoc:
    doc_id: str
    root: str
    child: str
    top2: str


def _listing(mcp, email: str, doc_id: str, **extra) -> dict:
    args = {"user_google_email": email, "document_id": doc_id}
    args.update(extra)
    return tool_json(mcp.call_tool("list_document_suggestions", args))


def _review_view(mcp, email: str, doc_id: str, **extra) -> dict:
    args = {"user_google_email": email, "document_id": doc_id}
    args.update(extra)
    return tool_json(mcp.call_tool("get_doc_review_view", args))


def _create_tab(mcp, email: str, doc_id: str, title: str, index: int, parent=None):
    args = {
        "user_google_email": email,
        "document_id": doc_id,
        "action": "create",
        "title": title,
        "index": index,
    }
    if parent is not None:
        args["parent_tab_id"] = parent
    created = tool_json(mcp.call_tool("manage_doc_tab", args))
    assert created["success"], created
    assert created["tab_id"], (
        "manage_doc_tab(create) returned no tab_id: the addDocumentTab "
        f"response member changed shape. Full response: {created}"
    )
    return created["tab_id"]


def _fill(mcp, email: str, doc_id: str, tab_id: str, text: str) -> None:
    confirmation = tool_text(
        mcp.call_tool(
            "modify_doc_text",
            {
                "user_google_email": email,
                "document_id": doc_id,
                "start_index": 1,
                "text": text,
                "tab_id": tab_id,
            },
        )
    )
    assert "Error" not in confirmation, confirmation


def _suggest(mcp, email: str, doc_id: str, tab_id: str, text: str, index: int = 1):
    return tool_json(
        mcp.call_tool(
            "suggest_doc_edit",
            {
                "user_google_email": email,
                "document_id": doc_id,
                "start_index": index,
                "text": text,
                "tab_id": tab_id,
            },
        )
    )


def _comment(mcp, email: str, doc_id: str, tab_id: str, content: str):
    return tool_json(
        mcp.call_tool(
            "create_anchored_doc_comment",
            {
                "user_google_email": email,
                "document_id": doc_id,
                "content": content,
                "start_index": 1,
                "end_index": 5,
                "tab_id": tab_id,
            },
        )
    )


@pytest.fixture
def nested_doc(mcp, ga_auth, make_scratch_doc) -> TabbedDoc:
    """A document shaped

        t.0  "Tab 1"            (root, holds ROOT_TEXT)
          +- <child>  "Nested"  (nested under the root, CHILD_TEXT)
        <top2> "Top level two"  (a sibling of the root, TOP2_TEXT)

    which is the smallest shape that separates "nested" from "second tab":
    the child and the root's sibling are at DIFFERENT depths, and (see
    ``test_a_nested_tab_...``) at the same ``index``.
    """
    email = ga_auth.email
    doc_id = make_scratch_doc("-nested", content=ROOT_TEXT)

    root = _listing(mcp, email, doc_id)["tabs"][0]["tab_id"]
    assert root, "a preview read of a fresh document reported no tab id"
    top2 = _create_tab(mcp, email, doc_id, "Top level two", 1)
    child = _create_tab(mcp, email, doc_id, "Nested", 0, parent=root)
    REPORT.note(f"nested-tab document: root={root} child={child} top2={top2}")

    def _all_three():
        listing = _listing(mcp, email, doc_id)
        ids = {t["tab_id"] for t in listing.get("tabs") or []}
        return listing if {root, child, top2} <= ids else None

    poll_until(_all_three, timeout=30, description="all three tabs to appear")
    _fill(mcp, email, doc_id, child, CHILD_TEXT)
    _fill(mcp, email, doc_id, top2, TOP2_TEXT)
    return TabbedDoc(doc_id=doc_id, root=root, child=child, top2=top2)


# ---------------------------------------------------------------------------
# Q1 -- nested tabs
# ---------------------------------------------------------------------------


@pytest.mark.e2e_ga
def test_a_nested_tab_is_creatable_without_the_preview_surface(
    mcp, ga_auth, make_scratch_doc
):
    """``addDocumentTab`` + ``parentTabId`` is GA, not preview-gated.

    This is the load-bearing half of the nested-tab question: if the API
    could not create a child tab, ``tab_documents()``' ``childTabs`` walk
    would be unreachable code and the only way to exercise it would be the
    Docs UI. It can, and ``inspect_doc_structure`` -- a plain
    ``documents.get(includeTabsContent=True)`` -- reports the child under
    its parent's ``child_tabs``.
    """
    email = ga_auth.email
    doc_id = make_scratch_doc("-nested-ga", content=ROOT_TEXT)
    root = "t.0"

    child_id = _create_tab(mcp, email, doc_id, "Nested", 0, parent=root)

    def _nested():
        text = tool_text(
            mcp.call_tool(
                "inspect_doc_structure",
                {"user_google_email": email, "document_id": doc_id},
            )
        )
        blob = text[text.index("{") : text.rindex("}") + 1]
        structure = json.loads(blob)
        tabs = structure.get("tabs") or []
        return structure if tabs and tabs[0].get("child_tabs") else None

    structure = poll_until(
        _nested, timeout=30, description="the child tab to appear under its parent"
    )
    REPORT.note(f"inspect_doc_structure tabs of a nested document: {structure['tabs']}")
    (parent,) = [t for t in structure["tabs"] if t["tab_id"] == root]
    assert [c["tab_id"] for c in parent["child_tabs"]] == [child_id], structure["tabs"]
    # And the child is NOT also reported as a top-level tab.
    assert child_id not in [t["tab_id"] for t in structure["tabs"]], structure["tabs"]


@pytest.mark.e2e_preview
def test_the_read_flattens_a_nested_tab_and_keeps_its_place_in_the_tree(
    preview_ready, mcp, ga_auth, nested_doc
):
    """Depth-first flattening, and the hierarchy the flattening would lose.

    ``index`` is a tab's position among its SIBLINGS, so this document has
    two tabs at ``index: 0`` -- the root and the root's child. A flat
    inventory carrying only ``(tab_id, title, index)`` therefore presents the
    nested tab as a second top-level tab colliding with the first, which is
    why ``parent_tab_id``/``nesting_level`` are on every entry.
    """
    email = ga_auth.email
    tabs = _listing(mcp, email, nested_doc.doc_id)["tabs"]
    REPORT.note(f"nested-document tab inventory: {tabs!r}")

    by_id = {t["tab_id"]: t for t in tabs}
    assert set(by_id) == {nested_doc.root, nested_doc.child, nested_doc.top2}, tabs
    # Depth-first: a child follows its parent, before the parent's sibling.
    assert [t["tab_id"] for t in tabs] == [
        nested_doc.root,
        nested_doc.child,
        nested_doc.top2,
    ], tabs

    assert by_id[nested_doc.root]["parent_tab_id"] is None, tabs
    assert by_id[nested_doc.root]["nesting_level"] == 0, tabs
    assert by_id[nested_doc.top2]["parent_tab_id"] is None, tabs
    assert by_id[nested_doc.top2]["nesting_level"] == 0, tabs
    assert by_id[nested_doc.child]["parent_tab_id"] == nested_doc.root, tabs
    assert by_id[nested_doc.child]["nesting_level"] == 1, tabs

    # The collision those two fields exist to resolve, asserted rather than
    # asserted-around: prod really does hand back two tabs at index 0.
    assert by_id[nested_doc.root]["index"] == by_id[nested_doc.child]["index"] == 0, (
        tabs
    )

    # Every tab's own prose is in the review view, each behind its own
    # ``===== tab_id: ... =====`` marker -- the child included.
    view = _review_view(mcp, email, nested_doc.doc_id, include_comments=False)
    body = view["body_text"]
    for tab_id, text in (
        (nested_doc.root, ROOT_TEXT),
        (nested_doc.child, CHILD_TEXT),
        (nested_doc.top2, TOP2_TEXT),
    ):
        assert f"tab_id: {tab_id}" in body, body[:600]
        assert text in body, body[:600]


@pytest.mark.e2e_preview
def test_a_child_tab_is_a_full_coordinate_space_of_its_own(
    preview_ready, mcp, ga_auth, nested_doc
):
    """A suggestion in a NESTED tab is found, addressed to that tab, and
    numbered from that tab's own start.

    The same local index is written in the root and in its child, so a
    reading that flattened the two index spaces together would have to
    return the wrong pre/post text for one of them.
    """
    email = ga_auth.email
    for tab_id, marker in ((nested_doc.child, "CHILD "), (nested_doc.root, "ROOT ")):
        response = _suggest(mcp, email, nested_doc.doc_id, tab_id, marker, index=1)
        assert response["mode"] == "insertion", response

    def _both():
        listing = _listing(mcp, email, nested_doc.doc_id, fields="full", page_size=50)
        found = {r["tab_id"] for r in listing["suggestions"]}
        return listing if {nested_doc.root, nested_doc.child} <= found else None

    listing = poll_until(
        _both, timeout=30, description="a suggestion in the root and in its child"
    )
    cards = {r["tab_id"]: r for r in listing["suggestions"]}
    REPORT.note(
        "nested-tab cards: "
        + repr(
            [
                (
                    r["suggestion_id"],
                    r["tab_id"],
                    r["start_index"],
                    r["context_after"][:24],
                )
                for r in listing["suggestions"]
            ]
        )
    )

    child_card = cards[nested_doc.child]
    root_card = cards[nested_doc.root]
    # Same local index, different tab, different characters after it.
    assert child_card["start_index"] == root_card["start_index"] == 1, listing
    assert child_card["post_text"] == "CHILD ", child_card
    assert root_card["post_text"] == "ROOT ", root_card
    assert child_card["context_after"].startswith(CHILD_TEXT[:20]), child_card
    assert root_card["context_after"].startswith(ROOT_TEXT[:20]), root_card
    assert child_card["segment"] == "body", child_card

    # And the child tab is usable as a filter, like any other tab.
    scoped = _listing(mcp, email, nested_doc.doc_id, tab_id=nested_doc.child)
    assert {r["tab_id"] for r in scoped["suggestions"]} == {nested_doc.child}, scoped
    assert scoped["matched_count"] == 1, scoped

    # An index window resolved IN the child answers about the child only.
    windowed = _listing(
        mcp,
        email,
        nested_doc.doc_id,
        tab_id=nested_doc.child,
        start_index=0,
        end_index=500,
    )
    assert windowed["filters"]["range_scope"]["tab_id"] == nested_doc.child, windowed
    assert {r["suggestion_id"] for r in windowed["suggestions"]} == {
        child_card["suggestion_id"]
    }, windowed


# ---------------------------------------------------------------------------
# Q2 -- tab attribution of threads
# ---------------------------------------------------------------------------


@pytest.mark.e2e_preview
def test_no_suggestion_id_is_claimed_by_more_than_one_tab(
    preview_ready, mcp, ga_auth, nested_doc
):
    """The premise the whole attribution strategy rests on.

    The top-level ``suggestions[]`` array carries no tab field, so the repo
    places a suggestion in the tab whose body carries its id. That is only
    sound if an id lives in exactly one tab -- one card per tab here, three
    tabs, three distinct ids, each reported against one tab.
    """
    email = ga_auth.email
    markers = {
        nested_doc.root: "R ",
        nested_doc.child: "C ",
        nested_doc.top2: "T ",
    }
    for tab_id, marker in markers.items():
        _suggest(mcp, email, nested_doc.doc_id, tab_id, marker, index=1)

    def _three():
        listing = _listing(mcp, email, nested_doc.doc_id, page_size=50)
        return listing if listing["suggestion_count"] >= 3 else None

    listing = poll_until(
        _three, timeout=30, description="a suggestion in each of the three tabs"
    )
    records = listing["suggestions"]
    assert listing["suggestion_count"] == 3, listing
    assert listing["returned_count"] == 3, listing

    ids = [r["suggestion_id"] for r in records]
    assert len(set(ids)) == 3, f"prod reused a suggestion id across tabs: {records}"
    placement = {r["suggestion_id"]: r["tab_id"] for r in records}
    assert set(placement.values()) == set(markers), placement
    REPORT.note(f"suggestion id -> tab placement: {placement!r}")

    # Each tab, asked on its own, claims exactly one of them; the three
    # per-tab answers partition the document with nothing left over and
    # nothing counted twice.
    partition: dict[str, set[str]] = {}
    for tab_id in markers:
        scoped = _listing(mcp, email, nested_doc.doc_id, tab_id=tab_id)
        partition[tab_id] = {r["suggestion_id"] for r in scoped["suggestions"]}
        assert len(partition[tab_id]) == 1, scoped
    assert set().union(*partition.values()) == set(ids), partition
    assert sum(len(v) for v in partition.values()) == 3, partition


@pytest.mark.e2e_preview
def test_a_comment_is_placed_in_the_tab_it_was_anchored_in(
    preview_ready, mcp, ga_auth, nested_doc
):
    """Comment threads carry no tab field either -- their ``anchorId`` does.

    Every tab's ``documentTab.commentAnchors`` is a map of the anchors living
    in THAT tab, and the maps are disjoint, so the anchor is the join. Three
    comments with identical anchor ranges ``[1, 5)`` in three different tabs:
    without the join every one of them would be indistinguishable, and the
    review view would report three unplaced threads on a document whose tabs
    are the thing a reviewer navigates by.
    """
    email = ga_auth.email
    expected = {}
    for tab_id, label in (
        (nested_doc.root, "root"),
        (nested_doc.child, "child"),
        (nested_doc.top2, "top2"),
    ):
        created = _comment(
            mcp, email, nested_doc.doc_id, tab_id, f"comment in the {label} tab"
        )
        assert created["comment_id"], created
        expected[created["comment_id"]] = tab_id

    def _all_three():
        view = _review_view(mcp, email, nested_doc.doc_id, include_comments=True)
        return view if len(view["comments"]) >= 3 else None

    view = poll_until(_all_three, timeout=30, description="three comment threads")
    placement = {c["comment_id"]: c["tab_id"] for c in view["comments"]}
    REPORT.note(
        "comment placement (thread carries no tab field; joined on anchorId): "
        + repr(
            [(c["comment_id"], c["anchor_id"], c["tab_id"]) for c in view["comments"]]
        )
    )
    assert placement == expected, (
        "a comment was attributed to the wrong tab, or to none: the "
        f"anchorId -> commentAnchors join is off. got={placement} "
        f"expected={expected}"
    )
    # Anchors are per-comment and never shared, which is what makes the join
    # a function rather than a lookup that can collide.
    anchors = [c["anchor_id"] for c in view["comments"]]
    assert all(anchors) and len(set(anchors)) == 3, view["comments"]


@pytest.mark.e2e_preview
def test_the_ga_fallback_cannot_see_a_nested_tab_and_never_claims_to(
    preview_ready, mcp, degraded_read_mcp, ga_auth, nested_doc
):
    """Why ``complete=False`` is not a diagnostic on a nested document.

    ``documents.get`` without ``includeTabsContent`` answers with ONE unnamed
    body -- the root tab's -- so a suggestion sitting in a child tab is not
    "resolved", it is unseen. The degraded read must therefore report no
    tabs, not the tabs it happens to have walked, and must refuse a tab-named
    query rather than answering it empty.
    """
    email = ga_auth.email
    _suggest(mcp, email, nested_doc.doc_id, nested_doc.child, "CHILD ", index=1)
    _suggest(mcp, email, nested_doc.doc_id, nested_doc.root, "ROOT ", index=1)

    def _both():
        listing = _listing(mcp, email, nested_doc.doc_id, page_size=50)
        found = {r["tab_id"] for r in listing["suggestions"]}
        return listing if {nested_doc.root, nested_doc.child} <= found else None

    healthy = poll_until(_both, timeout=30, description="both suggestions")
    assert healthy["suggestion_count"] == 2, healthy

    degraded = _listing(degraded_read_mcp, email, nested_doc.doc_id, page_size=50)
    REPORT.note(
        "degraded read of a nested-tab document: "
        f"read_source={degraded['read_source']!r} tabs={degraded['tabs']!r} "
        f"suggestion_count={degraded['suggestion_count']!r}"
    )
    assert degraded["read_source"] == "ga_documents_get", degraded
    assert degraded["tabs"] == [], degraded
    # Only the root tab's card is visible, and every record is tab-less.
    assert degraded["suggestion_count"] == 1, degraded
    assert [r["tab_id"] for r in degraded["suggestions"]] == [None], degraded
    assert degraded["degraded_notice"], degraded

    # A tab-named query is refused, not answered "0 matched": the child tab
    # id is one THIS SERVER printed on the healthy read a moment ago.
    refused = degraded_read_mcp.expect_tool_error(
        "list_document_suggestions",
        {
            "user_google_email": email,
            "document_id": nested_doc.doc_id,
            "tab_id": nested_doc.child,
        },
    )
    assert "cannot be filtered on this read" in refused, refused[:400]
