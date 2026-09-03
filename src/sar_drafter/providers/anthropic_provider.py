# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Claude via the Anthropic Messages API.

The internal message format is already Claude-shaped, so translation is nearly a
pass-through. Requires the ``anthropic`` package and an ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from .base import AssistantTurn, LLMProvider, ToolUse

DEFAULT_MODEL = "claude-3-5-sonnet-20241022"


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        api_key: str = "",
    ):
        try:
            import anthropic  # noqa: F401
        except ImportError as exc:  # pragma: no cover - env dependent
            raise RuntimeError(
                "The 'anthropic' package is required for the Anthropic provider. "
                "Install with `pip install anthropic`, or use --provider mock."
            ) from exc
        import anthropic

        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))

    @staticmethod
    def _to_anthropic_content(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for b in blocks:
            btype = b.get("type")
            if btype == "text":
                out.append({"type": "text", "text": b.get("text", "")})
            elif btype == "tool_use":
                out.append(
                    {
                        "type": "tool_use",
                        "id": b["id"],
                        "name": b["name"],
                        "input": b.get("input", {}),
                    }
                )
            elif btype == "tool_result":
                out.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": b["tool_use_id"],
                        "content": b.get("content", ""),
                    }
                )
        return out

    def converse(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> AssistantTurn:
        anthropic_messages = [
            {"role": m["role"], "content": self._to_anthropic_content(m["content"])}
            for m in messages
        ]
        anthropic_tools = [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t["input_schema"],
            }
            for t in tools
        ]

        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=system,
            tools=anthropic_tools,
            messages=anthropic_messages,
        )

        text_parts: List[str] = []
        tool_uses: List[ToolUse] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(ToolUse(id=block.id, name=block.name, input=block.input))

        return AssistantTurn(
            text="\n".join(text_parts).strip(),
            tool_uses=tool_uses,
            stop_reason=resp.stop_reason or "",
            raw=resp,
        )
