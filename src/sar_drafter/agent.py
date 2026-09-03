# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The investigation agent loop.

Drives a bounded tool-use conversation: the model investigates the case through
read-only tools, then calls ``submit_sar`` with a structured draft. The loop
enforces the safety and correctness boundaries:

  * every tool_use gets a tool_result (required by the model APIs);
  * ``submit_sar`` is intercepted and validated against the schema - an invalid
    draft is returned to the model to fix, within the round budget;
  * a hard cap on rounds bounds cost and latency.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .prompts import SYSTEM_PROMPT, build_task_message
from .providers.base import AssistantTurn, LLMProvider
from .schema import validate_sar
from .tools import TOOL_SPECS, CaseData, execute_tool


@dataclass
class InvestigationResult:
    case_id: str
    sar: Optional[Dict[str, Any]]
    valid: bool
    errors: List[str] = field(default_factory=list)
    rounds: int = 0
    trace: List[Dict[str, Any]] = field(default_factory=list)
    finished_reason: str = ""


def _assistant_content(turn: AssistantTurn) -> List[Dict[str, Any]]:
    content: List[Dict[str, Any]] = []
    if turn.text:
        content.append({"type": "text", "text": turn.text})
    for tu in turn.tool_uses:
        content.append({"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input})
    return content


def run_investigation(
    case: CaseData,
    provider: LLMProvider,
    max_rounds: int = 12,
) -> InvestigationResult:
    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": build_task_message(case.case_id)}]}
    ]
    trace: List[Dict[str, Any]] = []
    final_sar: Optional[Dict[str, Any]] = None
    valid = False
    errors: List[str] = []
    finished_reason = "max_rounds_exhausted"

    rounds = 0
    for rounds in range(1, max_rounds + 1):
        turn = provider.converse(SYSTEM_PROMPT, messages, TOOL_SPECS)
        messages.append({"role": "assistant", "content": _assistant_content(turn)})

        if not turn.wants_tools:
            # Model responded with prose only; nothing more to do.
            finished_reason = "model_stopped_without_submit"
            break

        tool_results: List[Dict[str, Any]] = []
        done = False
        for tu in turn.tool_uses:
            if tu.name == "submit_sar":
                candidate = tu.input or {}
                ok, errs = validate_sar(candidate)
                trace.append({"tool": "submit_sar", "valid": ok, "errors": errs})
                if ok:
                    final_sar, valid, errors = candidate, True, []
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": tu.id,
                         "content": json.dumps({"status": "accepted"})}
                    )
                    done = True
                    finished_reason = "submitted"
                else:
                    # Keep the best-effort draft, but ask the model to fix it.
                    final_sar, valid, errors = candidate, False, errs
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": tu.id,
                         "content": json.dumps({"status": "rejected", "errors": errs})}
                    )
            else:
                payload = execute_tool(case, tu.name, tu.input or {})
                trace.append({"tool": tu.name, "input": tu.input, "result_keys": list(payload.keys())})
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(payload)}
                )

        messages.append({"role": "user", "content": tool_results})
        if done:
            break

    return InvestigationResult(
        case_id=case.case_id,
        sar=final_sar,
        valid=valid,
        errors=errors,
        rounds=rounds,
        trace=trace,
        finished_reason=finished_reason,
    )
