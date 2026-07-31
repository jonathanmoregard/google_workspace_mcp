"""In-memory mock of Google Docs suggesting mode and its preview API surface.

Implements ``docs/plans/2026-07-30-suggestion-mock-spec.md`` so that the
suggestion semantics can be property-tested algebraically, and so that MCP
tool ergonomics can be exercised end to end without Workspace Developer
Preview enrollment.

Layout:

- :mod:`mockdocs.graphemes` -- the two unit systems (grapheme clusters,
  UTF-16 code units).
- :mod:`mockdocs.model` -- SPEC §2-§8, §11: char arrays, registry, edit ops,
  merge, resolve, projections, labels, invariants. One char array **per
  ``(tab, segment)``**: Docs numbers every such pair from its own start, so
  an index without its tab and segment is not an address (see the module
  docstring).
- :mod:`mockdocs.adapter` -- model <-> Docs API payloads; batchUpdate
  semantics including SUGGEST vs EDIT write modes and per-segment index
  resolution.
- :mod:`mockdocs.fake_services` -- duck-typed ``docs``/``drive`` service
  objects plus the process-wide :class:`~mockdocs.fake_services.FakeBackend`.
- :mod:`mockdocs.serve` -- mock-backed MCP server entry point (patches the
  auth seam; zero diffs to upstream files).
"""

from mockdocs.fake_services import FakeBackend, FakeDocsService, FakeDriveService
from mockdocs.model import (
    DEFAULT_TAB_ID,
    INDEX_BASE,
    MERGE_TOLERANCE,
    Char,
    MockDoc,
    MockDocsError,
    Segment,
    Suggestion,
    TabProperties,
)

__all__ = [
    "Char",
    "DEFAULT_TAB_ID",
    "FakeBackend",
    "FakeDocsService",
    "FakeDriveService",
    "INDEX_BASE",
    "MERGE_TOLERANCE",
    "MockDoc",
    "MockDocsError",
    "Segment",
    "Suggestion",
    "TabProperties",
]
