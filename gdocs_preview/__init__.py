"""docs_preview service: full-parity Google Docs/Drive review surface.

Generated API-parity tools live in ``gdocs_preview.generated`` (built by
``codegen/generate.py`` from committed discovery documents plus the Developer
Preview overlay). Curated ergonomics (suggestion diffing, capabilities probe,
reviewer-view read) are layered on top in hand-written modules.

Importing this package registers both layers on the shared FastMCP server
via decorator side effects.
"""

from gdocs_preview import curated_tools  # noqa: F401
from gdocs_preview import generated  # noqa: F401
