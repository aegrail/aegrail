"""Tests for Tool.to_openai_schema() and Tool.to_anthropic_schema().

Eliminates the DRY violation between aegrail Tool definitions and the
LLM-visible tool schemas that callers were previously declaring twice.
"""

from __future__ import annotations

import pytest

from aegrail import Tool


def _refund_tool() -> Tool:
    return Tool(
        name="issue_refund",
        fn=lambda order_id, amount_usd: None,
        description="Issue a refund for a customer order.",
        parameters={
            "order_id": {"type": "string", "description": "Order ID."},
            "amount_usd": {"type": "number", "description": "Amount in USD."},
        },
        required=["order_id", "amount_usd"],
    )


class TestOpenAISchema:
    def test_full_schema_round_trip(self) -> None:
        tool = _refund_tool()
        schema = tool.to_openai_schema()
        assert schema == {
            "type": "function",
            "function": {
                "name": "issue_refund",
                "description": "Issue a refund for a customer order.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "order_id": {"type": "string", "description": "Order ID."},
                        "amount_usd": {"type": "number", "description": "Amount in USD."},
                    },
                    "required": ["order_id", "amount_usd"],
                },
            },
        }

    def test_missing_description_omits_field(self) -> None:
        tool = Tool(
            name="t",
            fn=lambda: None,
            parameters={},
            required=[],
        )
        schema = tool.to_openai_schema()
        assert "description" not in schema["function"]

    def test_missing_parameters_raises(self) -> None:
        tool = Tool(name="t", fn=lambda: None)
        with pytest.raises(ValueError, match=r"Tool\.parameters must be set"):
            tool.to_openai_schema()

    def test_empty_required_is_empty_list(self) -> None:
        tool = Tool(
            name="t",
            fn=lambda x=None: None,
            parameters={"x": {"type": "string"}},
        )
        schema = tool.to_openai_schema()
        assert schema["function"]["parameters"]["required"] == []


class TestAnthropicSchema:
    def test_full_schema_round_trip(self) -> None:
        tool = _refund_tool()
        schema = tool.to_anthropic_schema()
        assert schema == {
            "name": "issue_refund",
            "description": "Issue a refund for a customer order.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "Order ID."},
                    "amount_usd": {"type": "number", "description": "Amount in USD."},
                },
                "required": ["order_id", "amount_usd"],
            },
        }

    def test_missing_description_omits_field(self) -> None:
        tool = Tool(
            name="t",
            fn=lambda: None,
            parameters={},
            required=[],
        )
        schema = tool.to_anthropic_schema()
        assert "description" not in schema

    def test_missing_parameters_raises(self) -> None:
        tool = Tool(name="t", fn=lambda: None)
        with pytest.raises(ValueError, match=r"Tool\.parameters must be set"):
            tool.to_anthropic_schema()
