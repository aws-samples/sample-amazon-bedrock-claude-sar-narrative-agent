#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Convenience launcher so you can run the CLI without setting PYTHONPATH.

    python run.py --provider mock --show-trace
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from sar_drafter.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
