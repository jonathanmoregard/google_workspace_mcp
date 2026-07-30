"""Parse ``claude -p --output-format stream-json`` into something gradeable.

One JSON object per line. The events this cares about:

``system`` / ``init``
    The tool surface the agent was actually given, and the MCP server's
    connection status. A run where ``mcp_servers`` never reaches
    ``connected`` is a harness failure, not an agent failure -- keeping the
    distinction is why this is parsed at all.
``assistant``
    ``tool_use`` blocks (name + input) and ``text`` blocks.
``user``
    ``tool_result`` blocks, carrying ``is_error`` and the result payload;
    matched back to their ``tool_use`` by id.
``result``
    Terminal event: ``subtype``, ``num_turns``, ``total_cost_usd``,
    ``duration_ms``, ``usage``, ``permission_denials`` and the final text.

Everything is tolerant of unknown event types and of blocks that are strings
rather than lists -- the stream format grows between CLI releases and a new
event must never take a batch down.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

#: Tools whose only effect is reading; used by the taxonomy's
#: "did the run verify its end state" rule.
READ_TOOL_SUFFIXES = frozenset(
    {
        "list_document_suggestions",
        "get_doc_review_view",
        "get_doc_content",
        "get_doc_as_markdown",
        "inspect_doc_structure",
        "list_document_comments",
        "list_docs_in_folder",
        "search_docs",
        "debug_docs_runtime_info",
        "debug_table_structure",
        "export_doc_to_pdf",
    }
)

#: Tools that mutate document content or its review state.
WRITE_TOOL_SUFFIXES = frozenset(
    {
        "suggest_doc_edit",
        "manage_document_suggestion",
        "reply_to_doc_thread",
        "create_anchored_doc_comment",
        "manage_document_comment",
        "modify_doc_text",
        "find_and_replace_doc",
        "insert_doc_elements",
        "insert_doc_image",
        "create_table_with_data",
        "update_paragraph_style",
        "update_doc_headers_footers",
        "manage_doc_tab",
        "batch_update_doc",
        "create_doc",
    }
)

#: Tools that apply an edit straight to the document instead of suggesting
#: it. Reaching for these in a suggesting-mode task is the canonical
#: wrong-tool-for-intent mistake.
DIRECT_EDIT_SUFFIXES = frozenset(
    {
        "modify_doc_text",
        "find_and_replace_doc",
        "insert_doc_elements",
        "batch_update_doc",
        "update_paragraph_style",
        "insert_doc_image",
        "create_table_with_data",
    }
)


#: Client-internal tools that are not part of the surface under measurement.
#: ``WaitForMcpServers`` shows up whenever the CLI is still connecting an MCP
#: server when the first turn starts; counting it would put a phantom tool at
#: the top of every usage table.
HARNESS_TOOL_NAMES = frozenset({"WaitForMcpServers"})


def split_tool_name(name: str) -> tuple[Optional[str], str]:
    """``mcp__server__tool`` -> ``("server", "tool")``; else ``(None, name)``."""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[1], parts[2]
        if len(parts) == 2:
            return parts[1], ""
    return None, name


@dataclass
class ToolCall:
    """One ``tool_use`` and the ``tool_result`` that answered it."""

    index: int
    name: str
    server: Optional[str]
    tool: str
    args: dict[str, Any]
    tool_use_id: str
    is_error: Optional[bool] = None
    result_text: str = ""
    answered: bool = False

    @property
    def failed(self) -> bool:
        return bool(self.is_error)

    @property
    def is_read(self) -> bool:
        return self.tool in READ_TOOL_SUFFIXES

    @property
    def is_write(self) -> bool:
        return self.tool in WRITE_TOOL_SUFFIXES

    @property
    def is_direct_edit(self) -> bool:
        return self.tool in DIRECT_EDIT_SUFFIXES

    @property
    def is_harness(self) -> bool:
        """A client-internal call, not something the agent chose to do."""
        return self.server is None and self.tool in HARNESS_TOOL_NAMES

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "tool": self.tool,
            "args": self.args,
            "ok": not self.failed,
            "answered": self.answered,
            "result_excerpt": self.result_text[:400],
        }


@dataclass
class Transcript:
    """Parsed stream-json for one run."""

    tool_calls: list[ToolCall] = field(default_factory=list)
    assistant_texts: list[str] = field(default_factory=list)
    available_tools: list[str] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    num_turns: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    result_text: str = ""
    subtype: str = ""
    terminal_reason: str = ""
    is_error: bool = False
    session_id: str = ""
    permission_denials: list[Any] = field(default_factory=list)
    malformed_lines: int = 0

    @property
    def final_text(self) -> str:
        return self.result_text or (self.assistant_texts[-1] if self.assistant_texts else "")

    @property
    def mcp_connected(self) -> bool:
        """True when a mock tool was actually reachable.

        ``system/init`` can report ``pending`` and still settle a moment
        later, so a successful MCP call counts as proof of connection.
        """
        if any(call.server for call in self.tool_calls):
            return True
        return any(s.get("status") == "connected" for s in self.mcp_servers)

    @property
    def agent_tool_calls(self) -> list[ToolCall]:
        """Calls the agent chose to make (client-internal ones excluded)."""
        return [call for call in self.tool_calls if not call.is_harness]

    @property
    def advertised_mcp_tools(self) -> list[str]:
        """MCP tool names ``system/init`` advertised.

        Empty when init fired before the server finished connecting, which is
        common -- so "was this name real?" must not be judged from an empty
        list.
        """
        return [t for t in self.available_tools if t.startswith("mcp__")]

    def tool_sequence(self) -> list[str]:
        return [call.tool for call in self.agent_tool_calls]

    def as_dict(self) -> dict[str, Any]:
        return {
            "num_turns": self.num_turns,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "usage": self.usage,
            "subtype": self.subtype,
            "terminal_reason": self.terminal_reason,
            "is_error": self.is_error,
            "session_id": self.session_id,
            "available_tools": self.available_tools,
            "mcp_servers": self.mcp_servers,
            "mcp_connected": self.mcp_connected,
            "permission_denials": self.permission_denials,
            "malformed_lines": self.malformed_lines,
            "final_text": self.final_text,
            "tool_calls": [c.as_dict() for c in self.tool_calls],
        }


def _content_blocks(message: Any) -> list[dict[str, Any]]:
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _result_payload_text(payload: Any) -> str:
    """Flatten a ``tool_result`` payload to text (string, or block list)."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        parts = []
        for block in payload:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
            else:
                parts.append(str(block))
        return "\n".join(p for p in parts if p)
    return json.dumps(payload, ensure_ascii=False)


def parse_stream_json(lines: Iterable[str]) -> Transcript:
    """Parse a stream-json transcript (an iterable of JSON lines)."""
    transcript = Transcript()
    by_id: dict[str, ToolCall] = {}

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            transcript.malformed_lines += 1
            continue
        if not isinstance(event, dict):
            transcript.malformed_lines += 1
            continue

        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            transcript.available_tools = list(event.get("tools") or [])
            servers = event.get("mcp_servers") or []
            transcript.mcp_servers = [s for s in servers if isinstance(s, dict)]
            transcript.session_id = event.get("session_id") or transcript.session_id

        elif etype == "assistant":
            for block in _content_blocks(event.get("message")):
                if block.get("type") == "tool_use":
                    server, tool = split_tool_name(str(block.get("name") or ""))
                    args = block.get("input")
                    call = ToolCall(
                        index=len(transcript.tool_calls),
                        name=str(block.get("name") or ""),
                        server=server,
                        tool=tool,
                        args=args if isinstance(args, dict) else {"_raw": args},
                        tool_use_id=str(block.get("id") or ""),
                    )
                    transcript.tool_calls.append(call)
                    if call.tool_use_id:
                        by_id[call.tool_use_id] = call
                elif block.get("type") == "text":
                    text = str(block.get("text") or "").strip()
                    if text:
                        transcript.assistant_texts.append(text)

        elif etype == "user":
            for block in _content_blocks(event.get("message")):
                if block.get("type") != "tool_result":
                    continue
                call = by_id.get(str(block.get("tool_use_id") or ""))
                if call is None:
                    continue
                call.answered = True
                call.is_error = bool(block.get("is_error"))
                call.result_text = _result_payload_text(block.get("content"))

        elif etype == "result" or (etype is None and "num_turns" in event):
            transcript.subtype = str(event.get("subtype") or "")
            transcript.terminal_reason = str(event.get("terminal_reason") or "")
            transcript.num_turns = int(event.get("num_turns") or 0)
            transcript.cost_usd = float(event.get("total_cost_usd") or 0.0)
            transcript.duration_ms = int(event.get("duration_ms") or 0)
            usage = event.get("usage")
            transcript.usage = usage if isinstance(usage, dict) else {}
            transcript.result_text = str(event.get("result") or "")
            transcript.is_error = bool(event.get("is_error"))
            denials = event.get("permission_denials") or []
            transcript.permission_denials = list(denials)
            transcript.session_id = event.get("session_id") or transcript.session_id

    return transcript


def parse_transcript_file(path: Any) -> Transcript:
    from pathlib import Path

    return parse_stream_json(Path(path).read_text(encoding="utf-8").splitlines())
