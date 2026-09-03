# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""SAR draft output schema and dependency-free validation.

The agent's final answer must conform to this structure. Keeping validation
self-contained (no third-party import required) guarantees the pipeline, tests,
and eval run offline with the bundled mock provider.

The JSON Schema in ``SAR_JSON_SCHEMA`` is also exported so it can be attached to
the model's ``submit_sar`` tool definition, so the model is guided toward the
same shape we validate against.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

FILING_RECOMMENDATIONS = ("recommend_file", "recommend_no_file", "needs_human_review")

# JSON Schema attached to the submit_sar tool so the model returns the right shape.
SAR_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "filing_recommendation",
        "confidence",
        "subjects",
        "activity_summary",
        "suspicious_typologies",
        "time_period",
        "total_suspicious_amount_usd",
        "narrative",
        "evidence",
        "red_flags",
        "unresolved_questions",
    ],
    "properties": {
        "filing_recommendation": {
            "type": "string",
            "enum": list(FILING_RECOMMENDATIONS),
            "description": (
                "The analyst-facing recommendation. Use 'needs_human_review' "
                "whenever the evidence is ambiguous. The tool never files."
            ),
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Calibrated confidence in the recommendation (0-1).",
        },
        "subjects": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "role"],
                "properties": {
                    "name": {"type": "string"},
                    "identifiers": {"type": "array", "items": {"type": "string"}},
                    "role": {"type": "string"},
                },
            },
        },
        "activity_summary": {
            "type": "string",
            "description": "One-paragraph plain-language summary of the suspicious activity.",
        },
        "suspicious_typologies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "AML typologies observed, e.g. 'structuring', 'rapid movement of funds'.",
        },
        "time_period": {
            "type": "object",
            "additionalProperties": False,
            "required": ["start", "end"],
            "properties": {
                "start": {"type": "string", "description": "YYYY-MM-DD"},
                "end": {"type": "string", "description": "YYYY-MM-DD"},
            },
        },
        "total_suspicious_amount_usd": {"type": "number", "minimum": 0},
        "narrative": {
            "type": "string",
            "description": (
                "The full SAR narrative prose covering who, what, when, where, "
                "and why/how the activity is suspicious. Every factual assertion "
                "must be supported by an entry in 'evidence'."
            ),
        },
        "evidence": {
            "type": "array",
            "description": "Maps each factual claim in the narrative to supporting transaction IDs.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim", "txn_ids"],
                "properties": {
                    "claim": {"type": "string"},
                    "txn_ids": {"type": "array", "items": {"type": "string"}},
                    "note": {"type": "string"},
                },
            },
        },
        "red_flags": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["flag", "supporting_txn_ids"],
                "properties": {
                    "flag": {"type": "string"},
                    "supporting_txn_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "unresolved_questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Open questions a human analyst should resolve before filing.",
        },
    },
}


def _is_date(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 10:
        return False
    parts = value.split("-")
    if len(parts) != 3:
        return False
    y, m, d = parts
    return y.isdigit() and m.isdigit() and d.isdigit() and len(y) == 4


def validate_sar(obj: Any) -> Tuple[bool, List[str]]:
    """Validate a SAR draft dict against the schema.

    Returns ``(is_valid, errors)``. Dependency-free so it works offline.
    """
    errors: List[str] = []

    if not isinstance(obj, dict):
        return False, ["SAR draft must be a JSON object."]

    required = SAR_JSON_SCHEMA["required"]
    for field in required:
        if field not in obj:
            errors.append(f"Missing required field: '{field}'.")

    rec = obj.get("filing_recommendation")
    if rec is not None and rec not in FILING_RECOMMENDATIONS:
        errors.append(
            f"filing_recommendation '{rec}' is not one of {FILING_RECOMMENDATIONS}."
        )

    conf = obj.get("confidence")
    if conf is not None and not (isinstance(conf, (int, float)) and 0.0 <= conf <= 1.0):
        errors.append("confidence must be a number between 0 and 1.")

    subjects = obj.get("subjects")
    if subjects is not None:
        if not isinstance(subjects, list) or not subjects:
            errors.append("subjects must be a non-empty list.")
        else:
            for i, s in enumerate(subjects):
                if not isinstance(s, dict) or "name" not in s or "role" not in s:
                    errors.append(f"subjects[{i}] must have 'name' and 'role'.")

    for str_field in ("activity_summary", "narrative"):
        val = obj.get(str_field)
        if val is not None and (not isinstance(val, str) or not val.strip()):
            errors.append(f"{str_field} must be a non-empty string.")

    typ = obj.get("suspicious_typologies")
    if typ is not None and not isinstance(typ, list):
        errors.append("suspicious_typologies must be a list.")

    tp = obj.get("time_period")
    if tp is not None:
        if not isinstance(tp, dict) or "start" not in tp or "end" not in tp:
            errors.append("time_period must have 'start' and 'end'.")
        else:
            if not _is_date(tp.get("start")):
                errors.append("time_period.start must be a YYYY-MM-DD date.")
            if not _is_date(tp.get("end")):
                errors.append("time_period.end must be a YYYY-MM-DD date.")

    amt = obj.get("total_suspicious_amount_usd")
    if amt is not None and not (isinstance(amt, (int, float)) and amt >= 0):
        errors.append("total_suspicious_amount_usd must be a non-negative number.")

    evidence = obj.get("evidence")
    if evidence is not None:
        if not isinstance(evidence, list):
            errors.append("evidence must be a list.")
        else:
            for i, e in enumerate(evidence):
                if not isinstance(e, dict) or "claim" not in e or "txn_ids" not in e:
                    errors.append(f"evidence[{i}] must have 'claim' and 'txn_ids'.")
                elif not isinstance(e["txn_ids"], list):
                    errors.append(f"evidence[{i}].txn_ids must be a list.")

    red_flags = obj.get("red_flags")
    if red_flags is not None and not isinstance(red_flags, list):
        errors.append("red_flags must be a list.")

    return (len(errors) == 0), errors


def all_cited_txn_ids(sar: Dict[str, Any]) -> List[str]:
    """Collect every transaction id referenced anywhere in the SAR draft."""
    ids: List[str] = []
    for e in sar.get("evidence", []) or []:
        if isinstance(e, dict):
            ids.extend(e.get("txn_ids", []) or [])
    for rf in sar.get("red_flags", []) or []:
        if isinstance(rf, dict):
            ids.extend(rf.get("supporting_txn_ids", []) or [])
    return ids
