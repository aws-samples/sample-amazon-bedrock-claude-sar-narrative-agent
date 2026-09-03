#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Evaluation harness for the SAR drafter.

This is what makes the project a serious sample rather than a demo: it measures
the qualities that matter for a compliance artifact, above all whether the draft
is *grounded* - every cited transaction id must actually exist in the case, with
zero fabrications.

Metrics per case:
  * schema_valid           - draft conforms to the SAR schema
  * citation_grounding     - fraction of cited txn ids that exist in the case (target 1.0)
  * hallucinated_txn_ids   - cited ids not present in the case (target 0)
  * typology_recall        - fraction of expected typology keywords present
  * subject_coverage       - fraction of expected subjects named in the draft
  * recommendation_match   - filing recommendation equals the expected label
  * period_within_range    - activity period falls inside the case's txn dates
  * amount_meets_floor     - total suspicious amount >= labeled floor

A case PASSES when it is schema-valid, fully grounded (grounding == 1.0 and no
hallucinated ids), recalls all expected typologies, and matches the expected
recommendation.

Run offline (mock provider) from the repo root:
    python eval/run_eval.py
    python eval/run_eval.py --provider bedrock --region us-east-1
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

# Make the src package importable when run from the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

from sar_drafter.agent import run_investigation  # noqa: E402
from sar_drafter.providers import get_provider  # noqa: E402
from sar_drafter.schema import all_cited_txn_ids, validate_sar  # noqa: E402
from sar_drafter.tools import CaseData  # noqa: E402


def _resolve(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(_REPO_ROOT, path)


def evaluate_case(expected: dict, provider) -> dict:
    case = CaseData.from_file(_resolve(expected["case_file"]))
    result = run_investigation(case, provider)
    sar = result.sar or {}

    schema_valid, _ = validate_sar(sar)

    case_txn_ids = set(case.transaction_ids())
    cited = all_cited_txn_ids(sar)
    hallucinated = sorted(set(cited) - case_txn_ids)
    # A draft that cites nothing (e.g. a legitimate recommend_no_file) is
    # vacuously grounded - what matters is that nothing was fabricated.
    grounding = (len([c for c in cited if c in case_txn_ids]) / len(cited)) if cited else 1.0

    narrative_blob = " ".join(
        [sar.get("narrative", ""), sar.get("activity_summary", "")]
        + (sar.get("suspicious_typologies", []) or [])
    ).lower()
    keywords = expected.get("expected_typology_keywords", [])
    matched_kw = [k for k in keywords if k.lower() in narrative_blob]
    typology_recall = (len(matched_kw) / len(keywords)) if keywords else 1.0

    subject_names = expected.get("expected_subject_names", [])
    draft_subjects_blob = json.dumps(sar.get("subjects", [])) + " " + sar.get("narrative", "")
    matched_subj = [n for n in subject_names if n.lower() in draft_subjects_blob.lower()]
    subject_coverage = (len(matched_subj) / len(subject_names)) if subject_names else 1.0

    rec_match = sar.get("filing_recommendation") == expected.get("expected_recommendation")

    tp = sar.get("time_period", {}) or {}
    dates = sorted(t.get("date") for t in case.transactions if t.get("date"))
    period_ok = bool(dates) and bool(tp.get("start")) and bool(tp.get("end")) \
        and tp["start"] >= dates[0] and tp["end"] <= dates[-1]

    amount_floor = expected.get("min_total_suspicious_amount_usd", 0)
    amount_ok = float(sar.get("total_suspicious_amount_usd", 0) or 0) >= amount_floor

    passed = (
        schema_valid
        and grounding == 1.0
        and not hallucinated
        and typology_recall == 1.0
        and rec_match
    )

    return {
        "case_id": case.case_id,
        "schema_valid": schema_valid,
        "citation_grounding": round(grounding, 3),
        "hallucinated_txn_ids": hallucinated,
        "typology_recall": round(typology_recall, 3),
        "subject_coverage": round(subject_coverage, 3),
        "recommendation_match": rec_match,
        "period_within_range": period_ok,
        "amount_meets_floor": amount_ok,
        "rounds": result.rounds,
        "passed": passed,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate SAR drafts.")
    parser.add_argument("--provider", default="mock", choices=["mock", "bedrock", "anthropic"])
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args(argv)

    kwargs = {"region_name": args.region} if args.provider == "bedrock" else {}
    provider = get_provider(args.provider, **kwargs)

    labeled_dir = os.path.join(_REPO_ROOT, "eval", "labeled")
    expected_files = sorted(glob.glob(os.path.join(labeled_dir, "*_expected.json")))
    if not expected_files:
        print("No labeled cases found in eval/labeled/.", file=sys.stderr)
        return 2

    results = []
    for ef in expected_files:
        with open(ef, "r", encoding="utf-8") as f:
            expected = json.load(f)
        results.append(evaluate_case(expected, provider))

    print(f"\nSAR drafter evaluation  (provider={args.provider})")
    print("=" * 72)
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"[{status}] {r['case_id']}")
        print(f"    schema_valid        : {r['schema_valid']}")
        print(f"    citation_grounding  : {r['citation_grounding']}  (hallucinated: {r['hallucinated_txn_ids'] or 'none'})")
        print(f"    typology_recall     : {r['typology_recall']}")
        print(f"    subject_coverage    : {r['subject_coverage']}")
        print(f"    recommendation_match: {r['recommendation_match']}")
        print(f"    period_within_range : {r['period_within_range']}")
        print(f"    amount_meets_floor  : {r['amount_meets_floor']}")
        print(f"    rounds              : {r['rounds']}")
    passed = sum(1 for r in results if r["passed"])
    print("-" * 72)
    print(f"Total: {passed}/{len(results)} cases passed.")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
