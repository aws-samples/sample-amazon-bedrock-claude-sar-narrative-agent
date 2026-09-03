# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Provider-agnostic message and provider types.

The internal message format is Claude-shaped (a list of content blocks per
turn) because that maps cleanly onto both the Anthropic Messages API and the
Bedrock Converse API. Each concrete provider translates this format to and from
its wire format.

Content block shapes (dicts) used internally:
  {"type": "text", "text": str}
  {"type": "tool_use", "id": str, "name": str, "input": dict}
  {"type": "tool_result", "tool_use_id": str, "content": str}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ToolUse:
    id: str
    name: str
    input: Dict[str, Any]


@dataclass
class AssistantTurn:
    """One assistant response: any text plus zero or more tool-use requests."""

    text: str = ""
    tool_uses: List[ToolUse] = field(default_factory=list)
    stop_reason: str = ""
    raw: Any = None

    @property
    def wants_tools(self) -> bool:
        return len(self.tool_uses) > 0


class LLMProvider:
    """Interface every provider implements."""

    name: str = "base"

    def converse(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> AssistantTurn:
        """Send one turn and return the assistant's response.

        ``messages`` is the running conversation in the internal format above.
        ``tools`` is the list of tool specs (name / description / input_schema).
        """
        raise NotImplementedError
