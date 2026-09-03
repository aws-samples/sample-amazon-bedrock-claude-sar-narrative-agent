# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for the SAR drafter. Run: python -m unittest -v (from repo root)."""

from __future__ import annotations

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

from sar_drafter.agent import run_investigation  # noqa: E402
from sar_drafter.providers import get_provider  # noqa: E402
from sar_drafter.schema import all_cited_txn_ids, validate_sar  # noqa: E402
from sar_drafter.tools import CaseData, default_case_path, execute_tool  # noqa: E402


def load_case() -> CaseData:
    return CaseData.from_file(default_case_path())


class SchemaTests(unittest.TestCase):
    def test_missing_fields_fail(self):
        ok, errors = validate_sar({})
        self.assertFalse(ok)
        self.assertTrue(any("Missing required field" in e for e in errors))

    def test_bad_recommendation_fails(self):
        ok, errors = validate_sar({"filing_recommendation": "definitely_file"})
        self.assertFalse(ok)
        self.assertTrue(any("filing_recommendation" in e for e in errors))

    def test_confidence_bounds(self):
        ok, errors = validate_sar({"confidence": 1.5})
        self.assertFalse(ok)
        self.assertTrue(any("confidence" in e for e in errors))


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.case = load_case()

    def test_case_overview(self):
        ov = execute_tool(self.case, "get_case_overview", {})
        self.assertEqual(ov["case_id"], "CASE-2026-00817")
        self.assertEqual(len(ov["subjects"]), 2)
        self.assertGreater(ov["transaction_count"], 0)

    def test_transaction_filter_by_type(self):
        res = execute_tool(self.case, "get_transactions", {"type": "cash_deposit"})
        self.assertEqual(res["count"], 12)
        for t in res["transactions"]:
            self.assertEqual(t["type"], "cash_deposit")
            self.assertLess(t["amount_usd"], 10000)

    def test_transaction_filter_by_amount_and_date(self):
        res = execute_tool(
            self.case,
            "get_transactions",
            {"type": "wire_out", "min_amount_usd": 30000, "start_date": "2026-07-18"},
        )
        self.assertEqual(res["count"], 3)

    def test_unknown_tool(self):
        res = execute_tool(self.case, "does_not_exist", {})
        self.assertIn("error", res)


class EndToEndMockTests(unittest.TestCase):
    def setUp(self):
        self.case = load_case()
        self.result = run_investigation(self.case, get_provider("mock"))

    def test_produces_valid_sar(self):
        self.assertIsNotNone(self.result.sar)
        self.assertTrue(self.result.valid, msg=str(self.result.errors))
        self.assertEqual(self.result.finished_reason, "submitted")

    def test_recommendation_is_file(self):
        self.assertEqual(self.result.sar["filing_recommendation"], "recommend_file")

    def test_all_citations_are_grounded(self):
        """The core guarantee: no fabricated transaction ids."""
        case_ids = set(self.case.transaction_ids())
        cited = all_cited_txn_ids(self.result.sar)
        self.assertTrue(cited, "expected the SAR to cite transactions")
        hallucinated = set(cited) - case_ids
        self.assertEqual(hallucinated, set(), f"fabricated txn ids: {hallucinated}")

    def test_typologies_present(self):
        blob = " ".join(self.result.sar["suspicious_typologies"]).lower()
        self.assertIn("structuring", blob)
        self.assertIn("rapid movement", blob)

    def test_investigates_before_drafting(self):
        # The agent should have called several read-only tools before submit_sar.
        tools_used = [s.get("tool") for s in self.result.trace]
        self.assertIn("get_case_overview", tools_used)
        self.assertIn("get_transactions", tools_used)
        self.assertIn("submit_sar", tools_used)


if __name__ == "__main__":
    unittest.main(verbosity=2)
