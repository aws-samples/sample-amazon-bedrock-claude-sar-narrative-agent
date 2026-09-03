# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Render a SAR draft dict into a readable, analyst-facing document."""

from __future__ import annotations

from typing import Any, Dict

DISCLAIMER = (
    "This SAR narrative is a DRAFT produced by an AI assistant for human review. "
    "It is decision-support, not a filing. A qualified BSA/AML analyst must "
    "verify every fact against source systems, exercise independent judgment, "
    "and make the filing determination. Do not file without human review."
)


def render_sar_markdown(sar: Dict[str, Any], case_id: str) -> str:
    lines = []
    lines.append(f"# SAR Draft - {case_id}")
    lines.append("")
    rec = sar.get("filing_recommendation", "unknown")
    conf = sar.get("confidence")
    conf_str = f"{conf:.0%}" if isinstance(conf, (int, float)) else "n/a"
    lines.append(f"**Recommendation:** `{rec}`  |  **Confidence:** {conf_str}")
    tp = sar.get("time_period", {}) or {}
    lines.append(
        f"**Activity period:** {tp.get('start', '?')} to {tp.get('end', '?')}  |  "
        f"**Total suspicious amount:** ${sar.get('total_suspicious_amount_usd', 0):,.2f}"
    )
    lines.append("")

    lines.append("## Subjects")
    for s in sar.get("subjects", []) or []:
        ids = ", ".join(s.get("identifiers", []) or [])
        ids = f" ({ids})" if ids else ""
        lines.append(f"- **{s.get('name', '?')}**{ids} - {s.get('role', '')}")
    lines.append("")

    typ = sar.get("suspicious_typologies", []) or []
    if typ:
        lines.append("## Typologies")
        lines.append(", ".join(typ))
        lines.append("")

    lines.append("## Activity summary")
    lines.append(sar.get("activity_summary", ""))
    lines.append("")

    lines.append("## Narrative")
    lines.append(sar.get("narrative", ""))
    lines.append("")

    red_flags = sar.get("red_flags", []) or []
    if red_flags:
        lines.append("## Red flags")
        for rf in red_flags:
            ids = ", ".join(rf.get("supporting_txn_ids", []) or [])
            lines.append(f"- {rf.get('flag', '')}  \n  _Evidence:_ {ids}")
        lines.append("")

    evidence = sar.get("evidence", []) or []
    if evidence:
        lines.append("## Evidence map")
        lines.append("")
        lines.append("| Claim | Supporting transactions |")
        lines.append("| --- | --- |")
        for e in evidence:
            claim = (e.get("claim", "") or "").replace("|", "\\|")
            ids = ", ".join(e.get("txn_ids", []) or [])
            lines.append(f"| {claim} | {ids} |")
        lines.append("")

    questions = sar.get("unresolved_questions", []) or []
    if questions:
        lines.append("## Unresolved questions for the analyst")
        for q in questions:
            lines.append(f"- {q}")
        lines.append("")

    lines.append("---")
    lines.append(f"_{DISCLAIMER}_")
    lines.append("")
    return "\n".join(lines)
