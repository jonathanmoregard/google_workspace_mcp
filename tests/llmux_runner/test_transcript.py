"""Parsing ``--output-format stream-json``.

The fixtures below are trimmed copies of real Claude Code 2.1.220 output.
"""

from __future__ import annotations

import json

from llmux.runner.transcript import parse_stream_json, split_tool_name


def _line(payload: dict) -> str:
    return json.dumps(payload)


INIT = _line(
    {
        "type": "system",
        "subtype": "init",
        "session_id": "sess-1",
        "tools": [
            "mcp__gdocsmock__get_doc_review_view",
            "mcp__gdocsmock__suggest_doc_edit",
        ],
        "mcp_servers": [{"name": "gdocsmock", "status": "connected"}],
    }
)


def _assistant_tool_use(tool_id: str, name: str, args: dict) -> str:
    return _line(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": args}],
            },
        }
    )


def _tool_result(tool_id: str, text: str, is_error: bool = False) -> str:
    return _line(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "is_error": is_error,
                        "content": [{"type": "text", "text": text}],
                    }
                ],
            },
        }
    )


RESULT = _line(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 4,
        "duration_ms": 12345,
        "total_cost_usd": 0.2478748,
        "usage": {"input_tokens": 2, "output_tokens": 190},
        "result": "Done.",
        "permission_denials": [],
        "session_id": "sess-1",
    }
)


def test_split_tool_name():
    assert split_tool_name("mcp__gdocsmock__suggest_doc_edit") == (
        "gdocsmock",
        "suggest_doc_edit",
    )
    assert split_tool_name("Read") == (None, "Read")


def test_parses_calls_results_and_totals():
    transcript = parse_stream_json(
        [
            INIT,
            _assistant_tool_use("t1", "mcp__gdocsmock__get_doc_review_view", {"document_id": "d"}),
            _tool_result("t1", '{"body_text": "hi"}'),
            _assistant_tool_use(
                "t2", "mcp__gdocsmock__suggest_doc_edit", {"start_index": 0}
            ),
            _tool_result("t2", "start_index must be >= 1.", is_error=True),
            RESULT,
        ]
    )
    assert [c.tool for c in transcript.tool_calls] == [
        "get_doc_review_view",
        "suggest_doc_edit",
    ]
    assert transcript.tool_calls[0].is_read
    assert transcript.tool_calls[1].is_write
    assert transcript.tool_calls[0].failed is False
    assert transcript.tool_calls[1].failed is True
    assert "must be >= 1" in transcript.tool_calls[1].result_text
    assert transcript.tool_calls[1].args == {"start_index": 0}
    assert transcript.num_turns == 4
    assert transcript.cost_usd == 0.2478748
    assert transcript.duration_ms == 12345
    assert transcript.final_text == "Done."
    assert transcript.mcp_connected is True
    assert transcript.subtype == "success"


def test_string_content_and_unknown_events_are_tolerated():
    transcript = parse_stream_json(
        [
            _line({"type": "rate_limit_event", "rate_limit_info": {}}),
            _line({"type": "system", "subtype": "thinking_tokens"}),
            _line(
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": "plain string"},
                }
            ),
            "",
            "not json at all",
            RESULT,
        ]
    )
    assert transcript.assistant_texts == ["plain string"]
    assert transcript.malformed_lines == 1
    assert transcript.num_turns == 4


def test_result_event_without_a_type_field_is_still_the_result():
    """Some builds emit the terminal object with ``type`` late in the dict."""
    transcript = parse_stream_json(
        [_line({"num_turns": 2, "total_cost_usd": 0.1, "result": "ok", "subtype": "success"})]
    )
    assert transcript.num_turns == 2
    assert transcript.final_text == "ok"


def test_mcp_connected_is_false_when_nothing_connected_and_nothing_called():
    transcript = parse_stream_json(
        [
            _line(
                {
                    "type": "system",
                    "subtype": "init",
                    "tools": [],
                    "mcp_servers": [{"name": "gdocsmock", "status": "pending"}],
                }
            ),
            RESULT,
        ]
    )
    assert transcript.mcp_connected is False


def test_a_successful_mcp_call_proves_connection_even_if_init_said_pending():
    transcript = parse_stream_json(
        [
            _line(
                {
                    "type": "system",
                    "subtype": "init",
                    "tools": [],
                    "mcp_servers": [{"name": "gdocsmock", "status": "pending"}],
                }
            ),
            _assistant_tool_use("t1", "mcp__gdocsmock__get_doc_review_view", {}),
            _tool_result("t1", "{}"),
            RESULT,
        ]
    )
    assert transcript.mcp_connected is True


def test_client_internal_calls_are_kept_but_excluded_from_the_agent_sequence():
    """``WaitForMcpServers`` is the CLI waiting, not the agent choosing.

    Observed in a real run: it otherwise lands in the tool-usage table as a
    phantom tool of the surface under measurement.
    """
    transcript = parse_stream_json(
        [
            INIT,
            _assistant_tool_use("t0", "WaitForMcpServers", {}),
            _tool_result("t0", "connected"),
            _assistant_tool_use("t1", "mcp__gdocsmock__get_doc_review_view", {}),
            _tool_result("t1", "{}"),
            RESULT,
        ]
    )
    assert len(transcript.tool_calls) == 2
    assert transcript.tool_calls[0].is_harness is True
    assert [c.tool for c in transcript.agent_tool_calls] == ["get_doc_review_view"]
    assert transcript.tool_sequence() == ["get_doc_review_view"]


def test_advertised_mcp_tools_ignores_builtins():
    transcript = parse_stream_json(
        [
            _line(
                {
                    "type": "system",
                    "subtype": "init",
                    "tools": ["WaitForMcpServers"],
                    "mcp_servers": [{"name": "gdocsmock", "status": "pending"}],
                }
            ),
            RESULT,
        ]
    )
    assert transcript.available_tools == ["WaitForMcpServers"]
    assert transcript.advertised_mcp_tools == []


def test_unanswered_tool_call_is_recorded_as_unanswered():
    transcript = parse_stream_json(
        [INIT, _assistant_tool_use("t9", "mcp__gdocsmock__suggest_doc_edit", {})]
    )
    call = transcript.tool_calls[0]
    assert call.answered is False
    assert call.failed is False
