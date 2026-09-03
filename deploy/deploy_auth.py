#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Manage the sign-in page credentials for the SAR Copilot.

Authentication is a proper cookie-session login served by the Lambda (a branded
sign-in page, HMAC-signed session cookie, server-side gating). This script sets
the credentials as Lambda environment variables and migrates off any earlier
edge Basic-auth CloudFront function.

Usage (from the repo root):
    python deploy/deploy_auth.py                       # user 'analyst', random password
    python deploy/deploy_auth.py --user demo --password <password>
    python deploy/deploy_auth.py --remove              # disable the login gate

Setting credentials updates the Lambda only (no CloudFront propagation). If an
old edge Basic-auth function is still attached, it is detached and deleted,
which does re-propagate CloudFront (a few minutes) - one time only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
FUNCTION_NAME = "sar-drafter"
DIST_COMMENT = "sar-drafter test UI"
EDGE_FN_NAME = "sar-drafter-basic-auth"


def _current_env(lam) -> dict:
    cfg = lam.get_function_configuration(FunctionName=FUNCTION_NAME)
    return dict((cfg.get("Environment", {}) or {}).get("Variables", {}) or {})


def _update_env(lam, env: dict) -> None:
    lam.update_function_configuration(FunctionName=FUNCTION_NAME, Environment={"Variables": env})
    try:
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION_NAME)
    except Exception:
        pass


def set_lambda_auth(lam, user: str, password: str) -> None:
    env = _current_env(lam)
    env["SAR_AUTH_USER"] = user
    env["SAR_AUTH_PASSWORD_SHA256"] = hashlib.sha256(password.encode("utf-8")).hexdigest()
    env["SAR_AUTH_SECRET"] = env.get("SAR_AUTH_SECRET") or secrets.token_hex(32)
    _update_env(lam, env)
    print("  set login credentials on the Lambda")


def clear_lambda_auth(lam) -> None:
    env = _current_env(lam)
    for k in ("SAR_AUTH_USER", "SAR_AUTH_PASSWORD_SHA256", "SAR_AUTH_SECRET"):
        env.pop(k, None)
    _update_env(lam, env)
    print("  cleared login credentials (gate disabled)")


def detach_edge_auth(cf) -> None:
    """Remove any legacy edge Basic-auth function + its distribution association."""
    dist_id = None
    for page in cf.get_paginator("list_distributions").paginate():
        for item in (page.get("DistributionList", {}) or {}).get("Items", []) or []:
            if item.get("Comment") == DIST_COMMENT:
                dist_id = item["Id"]
    if dist_id:
        cfg = cf.get_distribution_config(Id=dist_id)
        dcb = cfg["DistributionConfig"]["DefaultCacheBehavior"]
        if dcb.get("FunctionAssociations", {}).get("Quantity", 0):
            dcb["FunctionAssociations"] = {"Quantity": 0}
            cf.update_distribution(Id=dist_id, IfMatch=cfg["ETag"], DistributionConfig=cfg["DistributionConfig"])
            print("  detached edge Basic-auth function (CloudFront re-propagating)")
    try:
        desc = cf.describe_function(Name=EDGE_FN_NAME, Stage="DEVELOPMENT")
        cf.delete_function(Name=EDGE_FN_NAME, IfMatch=desc["ETag"])
        print("  deleted edge Basic-auth function")
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("NoSuchFunctionExists",):
            print(f"  (edge function not deleted: {e.response['Error']['Code']})")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Manage the SAR Copilot sign-in credentials.")
    parser.add_argument("--user", default="analyst")
    parser.add_argument("--password", default=None)
    parser.add_argument("--remove", action="store_true")
    args = parser.parse_args(argv)

    session = boto3.session.Session(region_name=REGION)
    lam = session.client("lambda")
    cf = session.client("cloudfront")

    print("Migrating to the Lambda-served sign-in page...")
    detach_edge_auth(cf)

    if args.remove:
        clear_lambda_auth(lam)
        print("\nLogin gate disabled. The app is now open (still private behind CloudFront/OAC).")
        return 0

    generated = args.password is None
    password = args.password or secrets.token_urlsafe(12)
    set_lambda_auth(lam, args.user, password)

    # Never print the plaintext password (it would leak into terminal history,
    # shell logs, and CI/CD output). Only the SHA-256 hash is stored server-side.
    print("\n" + "=" * 60)
    print(f"Sign-in enabled for user: {args.user}")
    if generated:
        # Write the one-time generated secret to a local, restricted, gitignored
        # file (mode 0600) instead of stdout.
        path = os.path.join(os.getcwd(), "sar-credentials.local.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"username": args.user, "password": password}, f, indent=2)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        print("A random password was generated and written to:")
        print(f"  {path}  (mode 0600, gitignored)")
        print("Do NOT commit it or paste it into logs. Delete it once noted.")
    else:
        print("Password: as provided on the command line (not echoed).")
    print("=" * 60)
    print("Open the CloudFront URL -> you'll get the branded sign-in page.")
    print("Rotate later:  python deploy/deploy_auth.py --user <u> --password <p>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
