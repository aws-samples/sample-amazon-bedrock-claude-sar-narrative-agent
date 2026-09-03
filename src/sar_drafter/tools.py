# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Read-only investigation tools the agent calls to gather evidence.

Every tool reads from a single in-memory case object (loaded from JSON). Tools
are strictly read-only: the agent can look, correlate, and cite, but cannot
change data or take action in the world. This is the safety boundary for a
defensive, human-in-the-loop workflow.

Two things are exported:
  * ``TOOL_SPECS``      - provider-agnostic tool definitions (name / description /
                          JSON input schema) attached to the model request;
  * ``execute_tool``    - dispatch that runs a named tool against a case.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from .schema import SAR_JSON_SCHEMA


class CaseData:
    """Thin wrapper over a case dict with convenient accessors."""

    def __init__(self, raw: Dict[str, Any]):
        self.raw = raw

    @classmethod
    def from_file(cls, path: str) -> "CaseData":
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f))

    @property
    def case_id(self) -> str:
        return self.raw.get("case_id", "UNKNOWN")

    @property
    def transactions(self) -> List[Dict[str, Any]]:
        return self.raw.get("transactions", [])

    def transaction_ids(self) -> List[str]:
        return [t.get("txn_id") for t in self.transactions if t.get("txn_id")]

    def subject(self, subject_id: str) -> Optional[Dict[str, Any]]:
        for s in self.raw.get("subjects", []):
            if s.get("subject_id") == subject_id:
                return s
        return None


# --- tool implementations -------------------------------------------------

def _get_case_overview(case: CaseData, _inp: Dict[str, Any]) -> Dict[str, Any]:
    raw = case.raw
    return {
        "case_id": raw.get("case_id"),
        "opened_date": raw.get("opened_date"),
        "priority": raw.get("priority"),
        "alert_ids": raw.get("alert_ids", []),
        "analyst_notes": raw.get("analyst_notes"),
        "subjects": [
            {"subject_id": s.get("subject_id"), "name": s.get("name"), "type": s.get("type"), "role": s.get("role")}
            for s in raw.get("subjects", [])
        ],
        "accounts": [
            {"account_id": a.get("account_id"), "subject_id": a.get("subject_id"), "type": a.get("type")}
            for a in raw.get("accounts", [])
        ],
        "transaction_count": len(case.transactions),
        "has_prior_sars": bool(raw.get("prior_sars")),
    }


def _get_subject_profile(case: CaseData, inp: Dict[str, Any]) -> Dict[str, Any]:
    subject_id = inp.get("subject_id")
    subject = case.subject(subject_id)
    if subject is None:
        return {"error": f"No subject found with subject_id '{subject_id}'."}
    return subject


def _get_account_activity(case: CaseData, inp: Dict[str, Any]) -> Dict[str, Any]:
    account_id = inp.get("account_id")
    txns = [t for t in case.transactions if t.get("account_id") == account_id]
    if not txns:
        return {"error": f"No transactions found for account '{account_id}'."}

    by_type: Dict[str, Dict[str, Any]] = {}
    total_in = 0.0
    total_out = 0.0
    dates = sorted(t.get("date") for t in txns if t.get("date"))
    inflow_types = {"cash_deposit", "wire_in", "ach_credit", "check_deposit"}
    for t in txns:
        ttype = t.get("type", "unknown")
        amt = float(t.get("amount_usd", 0) or 0)
        bucket = by_type.setdefault(ttype, {"count": 0, "total_usd": 0.0})
        bucket["count"] += 1
        bucket["total_usd"] += amt
        if ttype in inflow_types:
            total_in += amt
        else:
            total_out += amt

    return {
        "account_id": account_id,
        "transaction_count": len(txns),
        "date_range": {"start": dates[0], "end": dates[-1]} if dates else None,
        "total_inflow_usd": round(total_in, 2),
        "total_outflow_usd": round(total_out, 2),
        "by_type": by_type,
    }


def _get_transactions(case: CaseData, inp: Dict[str, Any]) -> Dict[str, Any]:
    account_id = inp.get("account_id")
    ttype = inp.get("type")
    min_amt = inp.get("min_amount_usd")
    max_amt = inp.get("max_amount_usd")
    start = inp.get("start_date")
    end = inp.get("end_date")
    counterparty_contains = inp.get("counterparty_contains")

    results = []
    for t in case.transactions:
        if account_id and t.get("account_id") != account_id:
            continue
        if ttype and t.get("type") != ttype:
            continue
        amt = float(t.get("amount_usd", 0) or 0)
        if min_amt is not None and amt < float(min_amt):
            continue
        if max_amt is not None and amt > float(max_amt):
            continue
        date = t.get("date") or ""
        if start and date < start:
            continue
        if end and date > end:
            continue
        if counterparty_contains:
            cp = (t.get("counterparty") or "")
            if counterparty_contains.lower() not in cp.lower():
                continue
        results.append(t)

    results.sort(key=lambda t: (t.get("date") or "", t.get("txn_id") or ""))
    return {"count": len(results), "transactions": results}


def _get_alerts(case: CaseData, _inp: Dict[str, Any]) -> Dict[str, Any]:
    return {"alerts": case.raw.get("alerts", [])}


def _get_related_parties(case: CaseData, _inp: Dict[str, Any]) -> Dict[str, Any]:
    return {"related_parties": case.raw.get("related_parties", [])}


def _get_prior_sars(case: CaseData, _inp: Dict[str, Any]) -> Dict[str, Any]:
    return {"prior_sars": case.raw.get("prior_sars", [])}


def _lookup_watchlist(case: CaseData, inp: Dict[str, Any]) -> Dict[str, Any]:
    """Return any recorded watchlist hits for the named subject.

    In this offline sample, watchlist hits live on the subject records. A real
    deployment would swap this for a sanctions/PEP screening provider.
    """
    name = (inp.get("name") or "").strip().lower()
    hits: List[Dict[str, Any]] = []
    for s in case.raw.get("subjects", []):
        if s.get("name", "").strip().lower() == name or not name:
            for h in s.get("watchlist_hits", []) or []:
                hits.append({"subject": s.get("name"), **h})
    return {"name": inp.get("name"), "watchlist_hits": hits}


# --- registry -------------------------------------------------------------

_DISPATCH = {
    "get_case_overview": _get_case_overview,
    "get_subject_profile": _get_subject_profile,
    "get_account_activity": _get_account_activity,
    "get_transactions": _get_transactions,
    "get_alerts": _get_alerts,
    "get_related_parties": _get_related_parties,
    "get_prior_sars": _get_prior_sars,
    "lookup_watchlist": _lookup_watchlist,
}

TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "get_case_overview",
        "description": "Return the case header: subjects, accounts, alert ids, priority, analyst notes, and transaction count. Call this first.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_subject_profile",
        "description": "Return the full KYC profile for a subject, including expected activity, risk rating, beneficial owners, and any watchlist hits.",
        "input_schema": {
            "type": "object",
            "properties": {"subject_id": {"type": "string"}},
            "required": ["subject_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_account_activity",
        "description": "Return aggregate activity for an account: transaction count, date range, total inflow/outflow, and breakdown by transaction type.",
        "input_schema": {
            "type": "object",
            "properties": {"account_id": {"type": "string"}},
            "required": ["account_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_transactions",
        "description": "Return individual transactions matching optional filters. Each transaction includes its txn_id, which you must cite as evidence. Filter by account, type, amount range, date range, or counterparty substring.",
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "type": {"type": "string", "description": "e.g. cash_deposit, wire_out, ach_debit"},
                "min_amount_usd": {"type": "number"},
                "max_amount_usd": {"type": "number"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD inclusive"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD inclusive"},
                "counterparty_contains": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_alerts",
        "description": "Return the monitoring alerts that triggered this case, with their rules and descriptions.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_related_parties",
        "description": "Return counterparties and related entities on record for this case, including jurisdiction and relationship notes.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_prior_sars",
        "description": "Return any previously filed SARs associated with the subjects in this case.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "lookup_watchlist",
        "description": "Screen a subject name against recorded sanctions/PEP watchlist hits.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "submit_sar",
        "description": "Submit the final structured SAR draft for human review. Call exactly once, after gathering evidence. Every factual claim must be supported by transaction ids you retrieved.",
        "input_schema": SAR_JSON_SCHEMA,
    },
]


def execute_tool(case: CaseData, name: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Run a read-only investigation tool by name. ``submit_sar`` is handled by
    the agent loop, not here."""
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"error": f"Unknown tool '{name}'."}
    try:
        return fn(case, tool_input or {})
    except Exception as exc:  # defensive: tools must never crash the loop
        return {"error": f"Tool '{name}' failed: {exc}"}


def default_case_path() -> str:
    """Path to the bundled sample case.

    Works both in the repo (src/sar_drafter layout) and when the package is
    deployed with sample_data alongside it (e.g. Lambda /var/task). Tries known
    candidate roots and returns the first that exists, falling back to the
    repo-relative path.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    rel = os.path.join("sample_data", "cases", "case_001_structuring.json")
    candidate_roots = [
        os.path.abspath(os.path.join(here, "..", "..")),  # repo root (src layout)
        os.path.abspath(os.path.join(here, "..")),         # package parent (flat/Lambda)
        os.getcwd(),
    ]
    for root in candidate_roots:
        path = os.path.join(root, rel)
        if os.path.exists(path):
            return path
    return os.path.join(candidate_roots[0], rel)
