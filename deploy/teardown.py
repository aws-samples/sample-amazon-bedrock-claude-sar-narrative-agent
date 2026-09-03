#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Full teardown: remove every AWS resource this project created.

Deletes (idempotently, ignoring already-absent resources):
  * public web layer (Function URL + CloudFront) - via teardown_web
  * Lambda function            sar-drafter
  * EventBridge rule           sar-cases-object-created
  * S3 bucket                  sar-cases-<account>   (emptied first)
  * DynamoDB table             sar-drafts
  * IAM role                   sar-drafter-lambda-role

Usage (from the repo root):
    python deploy/teardown.py
    python deploy/teardown.py --yes      # skip the confirmation prompt
"""

from __future__ import annotations

import argparse
import sys

import boto3
from botocore.exceptions import ClientError

# deploy/ is on sys.path when run as a script, so this sibling import works.
from teardown_web import delete_function_url, teardown_cloudfront

REGION = "us-east-1"
FUNCTION_NAME = "sar-drafter"
TABLE_NAME = "sar-drafts"
ROLE_NAME = "sar-drafter-lambda-role"
RULE_NAME = "sar-cases-object-created"
INLINE_POLICY = "sar-drafter-bedrock-dynamo"
MANAGED_POLICY = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"


def _ignore_missing(fn, *codes):
    try:
        fn()
    except ClientError as e:
        if e.response["Error"]["Code"] not in codes:
            raise
        return False
    return True


def _delete_auth_function(cf) -> None:
    """Delete the Basic-auth CloudFront function (once the distribution that used
    it is gone, so it is no longer associated)."""
    try:
        desc = cf.describe_function(Name="sar-drafter-basic-auth", Stage="DEVELOPMENT")
        cf.delete_function(Name="sar-drafter-basic-auth", IfMatch=desc["ETag"])
        print("  deleted Basic-auth CloudFront function")
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("NoSuchFunctionExists",):
            print(f"  (auth function not deleted: {e.response['Error']['Code']})")


def delete_lambda(lam) -> None:
    if _ignore_missing(lambda: lam.delete_function(FunctionName=FUNCTION_NAME), "ResourceNotFoundException"):
        print("  deleted Lambda function")
    else:
        print("  Lambda function already absent")


def delete_rule(events) -> None:
    try:
        events.remove_targets(Rule=RULE_NAME, Ids=["sar-drafter"], Force=True)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    if _ignore_missing(lambda: events.delete_rule(Name=RULE_NAME), "ResourceNotFoundException"):
        print("  deleted EventBridge rule")
    else:
        print("  EventBridge rule already absent")


def delete_bucket(s3, account_id: str) -> None:
    bucket = f"sar-cases-{account_id}"
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchBucket", "403"):
            print("  S3 bucket already absent")
            return
        raise
    # Empty the bucket (objects + versions) before deleting.
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket):
        to_delete = []
        for k in ("Versions", "DeleteMarkers"):
            for obj in page.get(k, []) or []:
                to_delete.append({"Key": obj["Key"], "VersionId": obj["VersionId"]})
        if to_delete:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": to_delete})
    # Also handle unversioned listing just in case.
    paginator2 = s3.get_paginator("list_objects_v2")
    for page in paginator2.paginate(Bucket=bucket):
        objs = [{"Key": o["Key"]} for o in page.get("Contents", []) or []]
        if objs:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objs})
    s3.delete_bucket(Bucket=bucket)
    print("  emptied and deleted S3 bucket")


def delete_table(ddb) -> None:
    if _ignore_missing(lambda: ddb.delete_table(TableName=TABLE_NAME), "ResourceNotFoundException"):
        print("  deleting DynamoDB table...")
        try:
            ddb.get_waiter("table_not_exists").wait(TableName=TABLE_NAME)
        except Exception:
            pass
        print("  deleted DynamoDB table")
    else:
        print("  DynamoDB table already absent")


def delete_role(iam) -> None:
    _ignore_missing(lambda: iam.delete_role_policy(RoleName=ROLE_NAME, PolicyName=INLINE_POLICY),
                    "NoSuchEntity")
    _ignore_missing(lambda: iam.detach_role_policy(RoleName=ROLE_NAME, PolicyArn=MANAGED_POLICY),
                    "NoSuchEntity")
    if _ignore_missing(lambda: iam.delete_role(RoleName=ROLE_NAME), "NoSuchEntity"):
        print("  deleted IAM role")
    else:
        print("  IAM role already absent")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Tear down all SAR drafter AWS resources.")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation.")
    args = parser.parse_args(argv)

    session = boto3.session.Session(region_name=REGION)
    account_id = session.client("sts").get_caller_identity()["Account"]

    if not args.yes:
        print(f"This will DELETE all sar-drafter resources in account {account_id} / {REGION}.")
        if input("Type 'delete' to confirm: ").strip().lower() != "delete":
            print("Aborted.")
            return 1

    print("[1/6] Public web layer")
    delete_function_url(session.client("lambda"))
    teardown_cloudfront(session.client("cloudfront"))
    _delete_auth_function(session.client("cloudfront"))
    print("[2/6] Lambda function")
    delete_lambda(session.client("lambda"))
    print("[3/6] EventBridge rule")
    delete_rule(session.client("events"))
    print("[4/6] S3 cases bucket")
    delete_bucket(session.client("s3"), account_id)
    print("[5/6] DynamoDB table")
    delete_table(session.client("dynamodb"))
    print("[6/6] IAM role")
    delete_role(session.client("iam"))

    print("\nTeardown complete. If CloudFront was still propagating, re-run to finish its delete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
