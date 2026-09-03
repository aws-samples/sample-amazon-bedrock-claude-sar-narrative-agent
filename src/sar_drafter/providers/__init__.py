# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""LLM provider abstraction.

The agent loop is written once against a small provider-agnostic interface
(``LLMProvider`` in ``base``). Three implementations are bundled:

  * ``bedrock``  - Claude via the Amazon Bedrock Converse API (default for real runs);
  * ``anthropic`` - Claude via the Anthropic Messages API (fallback);
  * ``mock``     - a scripted, deterministic provider so the pipeline, tests, and
                   eval run offline with no credentials.

Select one with ``get_provider(name, **kwargs)``.
"""

from __future__ import annotations

from typing import Any

from .base import AssistantTurn, LLMProvider, ToolUse


def get_provider(name: str, **kwargs: Any) -> LLMProvider:
    name = (name or "mock").lower()
    if name == "mock":
        from .mock import MockProvider

        return MockProvider(**kwargs)
    if name == "bedrock":
        from .bedrock import BedrockProvider

        return BedrockProvider(**kwargs)
    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(**kwargs)
    raise ValueError(f"Unknown provider '{name}'. Choose from: mock, bedrock, anthropic.")


__all__ = ["get_provider", "LLMProvider", "AssistantTurn", "ToolUse"]
