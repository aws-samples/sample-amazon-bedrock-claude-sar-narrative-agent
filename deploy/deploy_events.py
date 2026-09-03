#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Event-driven ingestion: drop a case JSON in S3, get a SAR draft automatically.

Creates (idempotently):
  * an S3 bucket  sar-cases-<account>  with EventBridge notifications enabled;
  * an EventBridge rule that fires on "Object Created" in that bucket and targets
    the sar-drafter Lambda; and
  * the Lambda permission letting EventBridge invoke the function.

Upload any *.json case and the drafter runs and stores the result in DynamoDB.
All resources are tagged auto-delete=no.
"""

from __future__ import annotations

import json

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
FUNCTION_NAME = "sar-drafter"
RULE_NAME = "sar-cases-object-created"
TAGS = {"auto-delete": "no", "Project": "sar-drafter", "ManagedBy": "deploy_events.py"}


def ensure_bucket(s3, bucket: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
        print(f"  bucket exists: {bucket}")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code not in ("404", "NoSuchBucket", "403"):
            raise
        print(f"  creating bucket: {bucket}")
        s3.create_bucket(Bucket=bucket)  # us-east-1: no LocationConstraint
    s3.put_bucket_tagging(
        Bucket=bucket,
        Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in TAGS.items()]},
    )
    s3.put_bucket_notification_configuration(
        Bucket=bucket, NotificationConfiguration={"EventBridgeConfiguration": {}}
    )
    print("  EventBridge notifications enabled")


def ensure_rule(events, lam, bucket: str, account_id: str) -> None:
    pattern = {
        "source": ["aws.s3"],
        "detail-type": ["Object Created"],
        "detail": {"bucket": {"name": [bucket]}},
    }
    events.put_rule(
        Name=RULE_NAME,
        EventPattern=json.dumps(pattern),
        State="ENABLED",
        Description="Draft a SAR when a case JSON is uploaded to the cases bucket.",
        Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()],
    )
    rule_arn = f"arn:aws:events:{REGION}:{account_id}:rule/{RULE_NAME}"
    func_arn = lam.get_function(FunctionName=FUNCTION_NAME)["Configuration"]["FunctionArn"]

    events.put_targets(Rule=RULE_NAME, Targets=[{"Id": "sar-drafter", "Arn": func_arn}])
    print("  rule + target configured")

    try:
        lam.add_permission(
            FunctionName=FUNCTION_NAME,
            StatementId="AllowEventBridgeCasesRule",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=rule_arn,
        )
        print("  EventBridge invoke permission added")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceConflictException":
            raise
        print("  EventBridge invoke permission already present")


def main() -> int:
    session = boto3.session.Session(region_name=REGION)
    account_id = session.client("sts").get_caller_identity()["Account"]
    bucket = f"sar-cases-{account_id}"

    print("[1/2] S3 cases bucket")
    ensure_bucket(session.client("s3"), bucket)

    print("[2/2] EventBridge rule -> Lambda")
    ensure_rule(session.client("events"), session.client("lambda"), bucket, account_id)

    print("\nDone. Auto-draft by uploading a case:")
    print(f"  aws s3 cp sample_data/cases/case_001_structuring.json s3://{bucket}/incoming/case_001.json")
    print("Then check DynamoDB:")
    print("  aws dynamodb scan --table-name sar-drafts --region us-east-1 --select COUNT --query Count --output text")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
