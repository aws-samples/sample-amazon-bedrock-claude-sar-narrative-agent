#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Remove all public exposure of the SAR drafter.

Deletes the Lambda Function URL and disables + deletes the CloudFront
distribution. The secure core is left intact: direct Lambda invoke and the
S3/EventBridge auto-draft path (both non-public).

Idempotent and re-runnable. CloudFront must be disabled and fully propagated
before it can be deleted, which takes several minutes; if it is not ready yet,
this script disables it (stopping it from serving) and asks you to re-run later
to complete the delete.
"""

from __future__ import annotations

import time

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
FUNCTION_NAME = "sar-drafter"
DIST_COMMENT = "sar-drafter test UI"


def delete_function_url(lam) -> None:
    try:
        lam.delete_function_url_config(FunctionName=FUNCTION_NAME)
        print("  deleted Function URL config")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            print("  Function URL already absent")
        else:
            raise
    for sid in ("AllowPublicFunctionUrl",):
        try:
            lam.remove_permission(FunctionName=FUNCTION_NAME, StatementId=sid)
            print(f"  removed permission {sid}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
    # Drop the injected API URL env var (page no longer served).
    try:
        cfg = lam.get_function_configuration(FunctionName=FUNCTION_NAME)
        env = (cfg.get("Environment", {}) or {}).get("Variables", {}) or {}
        if "SAR_API_URL" in env:
            env.pop("SAR_API_URL")
            lam.update_function_configuration(FunctionName=FUNCTION_NAME, Environment={"Variables": env})
            print("  cleared SAR_API_URL env var")
    except ClientError:
        pass


def _find_distribution(cf):
    paginator = cf.get_paginator("list_distributions")
    for page in paginator.paginate():
        for item in (page.get("DistributionList", {}) or {}).get("Items", []) or []:
            if item.get("Comment") == DIST_COMMENT:
                return item["Id"]
    return None


def teardown_cloudfront(cf, wait_seconds: int = 90) -> None:
    dist_id = _find_distribution(cf)
    if not dist_id:
        print("  no CloudFront distribution found")
        return

    cfg_resp = cf.get_distribution_config(Id=dist_id)
    etag = cfg_resp["ETag"]
    config = cfg_resp["DistributionConfig"]

    if config.get("Enabled", False):
        config["Enabled"] = False
        cf.update_distribution(Id=dist_id, IfMatch=etag, DistributionConfig=config)
        print(f"  disabled distribution {dist_id} (stops serving)")
    else:
        print(f"  distribution {dist_id} already disabled")

    # Try to reach 'Deployed' so we can delete; bounded wait.
    print("  waiting for distribution to finish disabling (bounded)...")
    deadline = time.time() + wait_seconds
    status = None
    while time.time() < deadline:
        d = cf.get_distribution(Id=dist_id)
        status = d["Distribution"]["Status"]
        if status == "Deployed":
            break
        time.sleep(15)

    if status != "Deployed":
        print(f"  distribution not yet propagated (status={status}).")
        print("  It is DISABLED and not serving. Re-run this script later to delete it.")
        return

    etag = cf.get_distribution_config(Id=dist_id)["ETag"]
    cf.delete_distribution(Id=dist_id, IfMatch=etag)
    print(f"  deleted distribution {dist_id}")


def main() -> int:
    session = boto3.session.Session(region_name=REGION)
    print("[1/2] Lambda Function URL")
    delete_function_url(session.client("lambda"))
    print("[2/2] CloudFront distribution")
    teardown_cloudfront(session.client("cloudfront"))
    print("\nPublic exposure removed. Secure paths (direct invoke, S3/EventBridge) are intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
