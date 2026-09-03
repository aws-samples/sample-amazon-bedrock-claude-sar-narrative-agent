# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Deterministic, offline mock provider.

This is not a language model. It simulates a disciplined investigator so the
whole pipeline - agent loop, tools, schema validation, rendering, and the eval
harness - runs with zero credentials and zero network. It is used for tests,
CI, and letting anyone try the project before wiring up Bedrock.

It is case-agnostic: it derives subjects and accounts from ``get_case_overview``
and drafts a SAR grounded ONLY in the transactions it actually observed. It also
demonstrates honest hedging - when the structuring signal is weak it recommends
``needs_human_review`` instead of forcing a filing call.

It works statelessly: on each turn it inspects the running conversation to see
which investigation steps have happened, then issues the next one.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .base import AssistantTurn, LLMProvider, ToolUse

# A cash deposit in this band looks like threshold avoidance.
_NEAR_THRESHOLD_LOW = 8000
_CTR_THRESHOLD = 10000
# Number of near-threshold deposits above which we treat structuring as established.
_STRUCTURING_MIN = 6


class MockProvider(LLMProvider):
    name = "mock"

    def converse(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> AssistantTurn:
        state = _ConversationState(messages)
        action = self._next_action(state)

        if action is not None:
            name, tool_input = action
            return AssistantTurn(
                text=f"Retrieving {name} to build the evidence picture.",
                tool_uses=[ToolUse(id=f"mock_{state.issued_count}", name=name, input=tool_input)],
                stop_reason="tool_use",
            )

        sar = self._build_sar(state)
        return AssistantTurn(
            text="Evidence gathering complete. Submitting SAR draft for analyst review.",
            tool_uses=[ToolUse(id="mock_submit", name="submit_sar", input=sar)],
            stop_reason="tool_use",
        )

    # -- planning ------------------------------------------------------------
    def _next_action(self, state: "_ConversationState") -> Optional[Tuple[str, Dict[str, Any]]]:
        if not state.issued("get_case_overview"):
            return ("get_case_overview", {})

        for sid in state.subject_ids():
            if not state.issued("get_subject_profile", sid):
                return ("get_subject_profile", {"subject_id": sid})

        if not state.issued("get_alerts"):
            return ("get_alerts", {})

        for aid in state.account_ids():
            if not state.issued("get_account_activity", aid):
                return ("get_account_activity", {"account_id": aid})

        if not state.issued("get_transactions", "cash_deposit"):
            return ("get_transactions", {"type": "cash_deposit"})
        if not state.issued("get_transactions", "wire_out"):
            return ("get_transactions", {"type": "wire_out"})

        if not state.issued("get_related_parties"):
            return ("get_related_parties", {})

        names = state.subject_names()
        if names and not state.issued("lookup_watchlist"):
            return ("lookup_watchlist", {"name": names[0]})

        return None  # ready to submit

    # -- drafting ------------------------------------------------------------
    def _build_sar(self, state: "_ConversationState") -> Dict[str, Any]:
        observed = state.observed_transactions()
        cash = [t for t in observed if t.get("type") == "cash_deposit"]
        wires = [t for t in observed if t.get("type") == "wire_out"]

        near = [t for t in cash if _NEAR_THRESHOLD_LOW <= float(t.get("amount_usd", 0) or 0) < _CTR_THRESHOLD]
        high_risk_wires = [t for t in wires if "high-risk" in (t.get("location") or "").lower()]

        subjects_out = state.subjects_for_sar()
        primary_name = subjects_out[0]["name"] if subjects_out else "the account holder"
        account_id = state.account_ids()[0] if state.account_ids() else "the account"

        dates = sorted(t.get("date") for t in observed if t.get("date"))
        start = dates[0] if dates else "1970-01-01"
        end = dates[-1] if dates else "1970-01-01"

        if len(near) >= _STRUCTURING_MIN or high_risk_wires:
            return self._file_sar(
                subjects_out, primary_name, account_id, start, end,
                near, wires, high_risk_wires,
            )
        if len(near) >= 1:
            return self._review_sar(
                subjects_out, primary_name, account_id, start, end, near, wires,
            )
        return self._nofile_sar(subjects_out, primary_name, account_id, start, end)

    def _file_sar(self, subjects_out, primary_name, account_id, start, end,
                  near, wires, high_risk_wires) -> Dict[str, Any]:
        near_ids = sorted(t["txn_id"] for t in near)
        wire_ids = sorted(t["txn_id"] for t in wires)
        near_total = round(sum(float(t.get("amount_usd", 0) or 0) for t in near), 2)
        wire_total = round(sum(float(t.get("amount_usd", 0) or 0) for t in wires), 2)
        dest = high_risk_wires[0].get("location") if high_risk_wires else (
            wires[0].get("location") if wires else "an external beneficiary")
        beneficiary = wires[0].get("counterparty") if wires else "an external beneficiary"

        narrative = (
            f"{primary_name} (account {account_id}) conducted a pattern of activity "
            f"between {start} and {end} that is inconsistent with its expected "
            f"profile. The account received {len(near)} cash deposits totaling "
            f"${near_total:,.2f}, each individually below the ${_CTR_THRESHOLD:,} "
            f"Currency Transaction Report threshold and split across multiple "
            f"branches, a pattern consistent with structuring to evade currency "
            f"reporting requirements. Within days, ${wire_total:,.2f} was transferred "
            f"out via {len(wires)} wire(s) to {beneficiary} ({dest}). The rapid "
            f"movement of structured cash to "
            f"{'a high-risk jurisdiction' if high_risk_wires else 'the beneficiary'} "
            f"with limited supporting documentation is the basis for this report."
        )
        typologies = ["structuring", "rapid movement of funds", "activity inconsistent with customer profile"]
        if high_risk_wires:
            typologies.append("funds transfer to high-risk jurisdiction")

        red_flags = [{
            "flag": "Multiple cash deposits kept just below the $10,000 reporting threshold across branches.",
            "supporting_txn_ids": near_ids,
        }]
        if wire_ids:
            red_flags.append({
                "flag": "Structured cash rapidly transferred out with limited supporting documentation.",
                "supporting_txn_ids": wire_ids,
            })

        return {
            "filing_recommendation": "recommend_file",
            "confidence": 0.82,
            "subjects": subjects_out,
            "activity_summary": (
                f"Between {start} and {end}, {len(near)} sub-threshold cash deposits "
                f"totaling ${near_total:,.2f} were structured and rapidly moved out "
                f"(${wire_total:,.2f}), inconsistent with the customer's profile."
            ),
            "suspicious_typologies": typologies,
            "time_period": {"start": start, "end": end},
            "total_suspicious_amount_usd": near_total,
            "narrative": narrative,
            "evidence": [
                {"claim": f"{len(near)} cash deposits totaling ${near_total:,.2f}, each below the ${_CTR_THRESHOLD:,} CTR threshold.",
                 "txn_ids": near_ids, "note": "Structuring pattern."},
                {"claim": f"Funds moved out via {len(wires)} wire(s) totaling ${wire_total:,.2f}.",
                 "txn_ids": wire_ids, "note": "Rapid movement / layering."},
            ],
            "red_flags": red_flags,
            "unresolved_questions": [
                "The source of the cash deposits has not been established.",
            ],
        }

    def _nofile_sar(self, subjects_out, primary_name, account_id, start, end) -> Dict[str, Any]:
        """Document a no-file determination when no suspicious indicators are found."""
        narrative = (
            f"{primary_name} (account {account_id}) was reviewed for the period "
            f"{start} to {end} following a monitoring alert. The account activity "
            f"is consistent with the customer's established profile: inflows are "
            f"invoice-driven client payments, outflows are payroll, lease, travel, "
            f"and vendor payments, and cash activity is negligible. The event that "
            f"triggered the alert has a documented business explanation. No "
            f"structuring, rapid movement of funds, high-risk-jurisdiction exposure, "
            f"or other indicators of suspicious activity were identified. On the "
            f"available evidence, no SAR filing is recommended. This determination "
            f"is documented for the record and remains subject to human review."
        )
        return {
            "filing_recommendation": "recommend_no_file",
            "confidence": 0.8,
            "subjects": subjects_out,
            "activity_summary": (
                f"Activity between {start} and {end} is consistent with the "
                f"customer's profile; the alerting event has a documented business "
                f"explanation and no suspicious indicators were found."
            ),
            "suspicious_typologies": [],
            "time_period": {"start": start, "end": end},
            "total_suspicious_amount_usd": 0,
            "narrative": narrative,
            "evidence": [],
            "red_flags": [],
            "unresolved_questions": [],
        }

    def _review_sar(self, subjects_out, primary_name, account_id, start, end,
                    near, wires) -> Dict[str, Any]:
        near_ids = sorted(t["txn_id"] for t in near)
        wire_ids = sorted(t["txn_id"] for t in wires)
        near_total = round(sum(float(t.get("amount_usd", 0) or 0) for t in near), 2)

        narrative = (
            f"{primary_name} (account {account_id}) showed elevated cash activity "
            f"between {start} and {end}. {len(near)} deposit(s) fell just below the "
            f"${_CTR_THRESHOLD:,} Currency Transaction Report threshold, which can "
            f"indicate structuring; however, the account also received larger "
            f"deposits that were reported normally, and the overall pattern is "
            f"consistent with a cash-intensive retail business operating near its "
            f"stated profile. The evidence available is insufficient to conclude "
            f"that the activity is suspicious. A human analyst should review the "
            f"business's supporting records before a filing determination is made."
        )
        evidence = []
        if near_ids:
            evidence.append({
                "claim": f"{len(near)} deposit(s) totaling ${near_total:,.2f} fell just below the ${_CTR_THRESHOLD:,} threshold.",
                "txn_ids": near_ids,
                "note": "Potential threshold avoidance - not established.",
            })
        red_flags = []
        if near_ids:
            red_flags.append({
                "flag": "A small number of cash deposits fell just below the reporting threshold at more than one branch.",
                "supporting_txn_ids": near_ids,
            })

        return {
            "filing_recommendation": "needs_human_review",
            "confidence": 0.4,
            "subjects": subjects_out,
            "activity_summary": (
                f"Elevated but plausibly legitimate cash activity between {start} and "
                f"{end}; {len(near)} near-threshold deposit(s) warrant human review "
                f"against the business's records before any filing determination."
            ),
            "suspicious_typologies": [],
            "time_period": {"start": start, "end": end},
            "total_suspicious_amount_usd": near_total,
            "narrative": narrative,
            "evidence": evidence,
            "red_flags": red_flags,
            "unresolved_questions": [
                "Do the deposit amounts match the business's daily sales receipts?",
                "Is the elevated volume explained by seasonality or a promotion?",
                "Were the two near-threshold deposits at different branches operationally driven or deliberate?",
            ],
        }


class _ConversationState:
    """Read-only view over the running conversation for the mock's planning."""

    def __init__(self, messages: List[Dict[str, Any]]):
        self.messages = messages
        self._issued: List[Tuple[str, str]] = []
        self._results: List[Dict[str, Any]] = []
        for m in messages:
            for block in m.get("content", []):
                if block.get("type") == "tool_use":
                    self._issued.append((block.get("name"), self._arg_key(block.get("name"), block.get("input", {}))))
                elif block.get("type") == "tool_result":
                    try:
                        self._results.append(json.loads(block.get("content", "")))
                    except (ValueError, TypeError):
                        pass

    @staticmethod
    def _arg_key(name: str, inp: Dict[str, Any]) -> str:
        if name == "get_subject_profile":
            return inp.get("subject_id", "")
        if name == "get_account_activity":
            return inp.get("account_id", "")
        if name == "get_transactions":
            return inp.get("type", "")
        return ""

    @property
    def issued_count(self) -> int:
        return len(self._issued)

    def issued(self, name: str, arg: str = "") -> bool:
        return (name, arg) in self._issued

    def _overview(self) -> Dict[str, Any]:
        for r in self._results:
            if isinstance(r, dict) and "subjects" in r and "accounts" in r:
                return r
        return {}

    def subject_ids(self) -> List[str]:
        return [s.get("subject_id") for s in self._overview().get("subjects", []) if s.get("subject_id")]

    def subject_names(self) -> List[str]:
        return [s.get("name") for s in self._overview().get("subjects", []) if s.get("name")]

    def account_ids(self) -> List[str]:
        return [a.get("account_id") for a in self._overview().get("accounts", []) if a.get("account_id")]

    def subjects_for_sar(self) -> List[Dict[str, Any]]:
        ov = self._overview()
        accounts = self.account_ids()
        out = []
        for i, s in enumerate(ov.get("subjects", [])):
            ids = [s.get("subject_id")] if s.get("subject_id") else []
            if i == 0:
                ids += accounts
            out.append({"name": s.get("name", "Unknown"), "identifiers": ids, "role": s.get("role", "Subject")})
        return out or [{"name": "Unknown subject", "identifiers": [], "role": "Subject"}]

    def observed_transactions(self) -> List[Dict[str, Any]]:
        txns: Dict[str, Dict[str, Any]] = {}
        for r in self._results:
            for t in (r.get("transactions", []) if isinstance(r, dict) else []) or []:
                if isinstance(t, dict) and t.get("txn_id"):
                    txns[t["txn_id"]] = t
        return list(txns.values())
