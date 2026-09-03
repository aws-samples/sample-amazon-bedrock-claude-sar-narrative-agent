# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Command-line entrypoint: investigate a case and print a SAR draft.

Examples:
    # Offline, no credentials needed:
    python -m sar_drafter.cli --provider mock

    # Against Claude on Amazon Bedrock:
    python -m sar_drafter.cli --provider bedrock --region us-east-1

    # Against the Anthropic API:
    ANTHROPIC_API_KEY=... python -m sar_drafter.cli --provider anthropic
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from .agent import run_investigation
from .providers import get_provider
from .render import render_sar_markdown
from .tools import CaseData, default_case_path


def _build_provider(args: argparse.Namespace):
    kwargs = {}
    if args.model:
        kwargs["model_id" if args.provider == "bedrock" else "model"] = args.model
    if args.provider == "bedrock" and args.region:
        kwargs["region_name"] = args.region
    return get_provider(args.provider, **kwargs)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AML/SAR investigation narrative drafter")
    parser.add_argument("--provider", default="mock", choices=["mock", "bedrock", "anthropic"],
                        help="LLM provider (default: mock, fully offline).")
    parser.add_argument("--case", default=None, help="Path to a case JSON file (default: bundled sample).")
    parser.add_argument("--model", default=None, help="Override the model id/name for the provider.")
    parser.add_argument("--region", default="us-east-1", help="AWS region for the Bedrock provider.")
    parser.add_argument("--max-rounds", type=int, default=12, help="Max tool-use rounds.")
    parser.add_argument("--json", action="store_true", help="Print the raw SAR JSON instead of markdown.")
    parser.add_argument("--out", default=None, help="Write the rendered markdown to this file.")
    parser.add_argument("--show-trace", action="store_true", help="Print the investigation tool-call trace.")
    args = parser.parse_args(argv)

    case_path = args.case or default_case_path()
    try:
        case = CaseData.from_file(case_path)
    except FileNotFoundError:
        print(f"ERROR: case file not found: {case_path}", file=sys.stderr)
        return 2

    try:
        provider = _build_provider(args)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    result = run_investigation(case, provider, max_rounds=args.max_rounds)

    if args.show_trace:
        print("=== Investigation trace ===", file=sys.stderr)
        for i, step in enumerate(result.trace, 1):
            print(f"  {i:2d}. {step.get('tool')}  {step.get('input', '')}", file=sys.stderr)
        print(
            f"=== rounds={result.rounds} finished={result.finished_reason} "
            f"valid={result.valid} ===",
            file=sys.stderr,
        )

    if result.sar is None:
        print("ERROR: the agent produced no SAR draft.", file=sys.stderr)
        return 1

    if not result.valid:
        print("WARNING: SAR draft failed schema validation:", file=sys.stderr)
        for e in result.errors:
            print(f"  - {e}", file=sys.stderr)

    if args.json:
        output = json.dumps(result.sar, indent=2)
    else:
        output = render_sar_markdown(result.sar, result.case_id)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Wrote SAR draft to {args.out}", file=sys.stderr)
    else:
        print(output)

    return 0 if result.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
