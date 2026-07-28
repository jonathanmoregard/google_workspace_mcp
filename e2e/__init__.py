"""Blackbox e2e suite for the gdocs-review-mcp docs_preview surface.

Spawns the real MCP server as a stdio subprocess and drives it over the
MCP protocol against the real Google APIs. Credential-gated: with no
OAuth token present every test skips cleanly and loudly (see gating.py).
"""
