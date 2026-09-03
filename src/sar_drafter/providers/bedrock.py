# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Claude via the Amazon Bedrock Converse API.

Translates the internal Claude-shaped message format to and from the Bedrock
Converse wire format. Requires boto3 and AWS credentials with bedrock:InvokeModel
(or the Converse permission) for the chosen model.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from .base import AssistantTurn, LLMProvider, ToolUse

# Claude 3.5 Sonnet on Bedrock. Override via SarDrafter config / --model.
DEFAULT_MODEL_ID = "anthropic.claude-3-5-sonnet-20241022-v2:0"


class BedrockProvider(LLMProvider):
    name = "bedrock"

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL_ID,
        region_name: str = "us-east-1",
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ):
        try:
            import boto3  # noqa: F401
        except ImportError as exc:  # pragma: no cover - env dependent
            raise RuntimeError(
                "boto3 is required for the Bedrock provider. Install with "
                "`pip install boto3`, or use --provider mock for an offline run."
            ) from exc
        import boto3

        self.model_id = model_id
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._client = boto3.client("bedrock-runtime", region_name=region_name)

    # -- internal -> Converse ------------------------------------------------
    @staticmethod
    def _to_converse_content(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for b in blocks:
            btype = b.get("type")
            if btype == "text":
                out.append({"text": b.get("text", "")})
            elif btype == "tool_use":
                out.append(
                    {
                        "toolUse": {
                            "toolUseId": b["id"],
                            "name": b["name"],
                            "input": b.get("input", {}),
                        }
                    }
                )
            elif btype == "tool_result":
                out.append(
                    {
                        "toolResult": {
                            "toolUseId": b["tool_use_id"],
                            "content": [{"text": b.get("content", "")}],
                        }
                    }
                )
        return out

    def _to_converse_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {"role": m["role"], "content": self._to_converse_content(m["content"])}
            for m in messages
        ]

    @staticmethod
    def _to_tool_config(tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "tools": [
                {
                    "toolSpec": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "inputSchema": {"json": t["input_schema"]},
                    }
                }
                for t in tools
            ]
        }

    # -- request -------------------------------------------------------------
    def converse(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> AssistantTurn:
        resp = self._client.converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=self._to_converse_messages(messages),
            toolConfig=self._to_tool_config(tools),
            inferenceConfig={"maxTokens": self.max_tokens, "temperature": self.temperature},
        )

        message = resp.get("output", {}).get("message", {})
        text_parts: List[str] = []
        tool_uses: List[ToolUse] = []
        for block in message.get("content", []):
            if "text" in block:
                text_parts.append(block["text"])
            elif "toolUse" in block:
                tu = block["toolUse"]
                tool_uses.append(
                    ToolUse(id=tu["toolUseId"], name=tu["name"], input=tu.get("input", {}))
                )

        return AssistantTurn(
            text="\n".join(text_parts).strip(),
            tool_uses=tool_uses,
            stop_reason=resp.get("stopReason", ""),
            raw=resp,
        )
