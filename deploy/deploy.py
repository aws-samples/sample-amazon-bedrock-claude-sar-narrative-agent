#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Idempotent deployment of the SAR drafter to AWS Lambda + DynamoDB.

Creates (or updates) a scoped IAM role, a DynamoDB table for drafts, and a
Lambda function running the agent with the Bedrock (Claude) provider, then runs
a test invocation against the bundled synthetic case.

Every resource is tagged auto-delete=no so the account janitor does not reap it.

Usage (from the repo root):
    python deploy/deploy.py
    python deploy/deploy.py --model-id us.anthropic.claude-sonnet-4-5-20250929-v1:0
    python deploy/deploy.py --no-invoke        # deploy without the test invoke
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import zipfile

import boto3
from botocore.exceptions import ClientError

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGION = "us-east-1"

ROLE_NAME = "sar-drafter-lambda-role"
FUNCTION_NAME = "sar-drafter"
TABLE_NAME = "sar-drafts"
JOBS_TABLE = "sar-jobs"
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
RUNTIME = "python3.12"
HANDLER = "lambda_handler.handler"

TAGS = {"auto-delete": "no", "Project": "sar-drafter", "ManagedBy": "deploy.py"}

TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}
    ],
}


def inline_policy(account_id: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BedrockInvokeClaude",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                "Resource": [
                    "arn:aws:bedrock:*::foundation-model/anthropic.*",
                    f"arn:aws:bedrock:*:{account_id}:inference-profile/*",
                ],
            },
            {
                "Sid": "DraftsAndJobsTables",
                "Effect": "Allow",
                "Action": ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem"],
                "Resource": [
                    f"arn:aws:dynamodb:{REGION}:{account_id}:table/{TABLE_NAME}",
                    f"arn:aws:dynamodb:{REGION}:{account_id}:table/{TABLE_NAME}/index/*",
                    f"arn:aws:dynamodb:{REGION}:{account_id}:table/{JOBS_TABLE}",
                    f"arn:aws:dynamodb:{REGION}:{account_id}:table/{JOBS_TABLE}/index/*",
                ],
            },
            {
                "Sid": "ReadCaseObjects",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::sar-cases-{account_id}/*"],
            },
            {
                "Sid": "SelfInvokeForAsyncJobs",
                "Effect": "Allow",
                "Action": ["lambda:InvokeFunction"],
                "Resource": [f"arn:aws:lambda:{REGION}:{account_id}:function:{FUNCTION_NAME}"],
            },
        ],
    }


def ensure_role(iam, account_id: str) -> str:
    try:
        role = iam.get_role(RoleName=ROLE_NAME)
        print(f"  role exists: {ROLE_NAME}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        print(f"  creating role: {ROLE_NAME}")
        role = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(TRUST_POLICY),
            Description="Execution role for the SAR drafter Lambda.",
            Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()],
        )

    iam.attach_role_policy(
        RoleName=ROLE_NAME,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    )
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="sar-drafter-bedrock-dynamo",
        PolicyDocument=json.dumps(inline_policy(account_id)),
    )
    return role["Role"]["Arn"]


def ensure_table(ddb) -> None:
    try:
        ddb.describe_table(TableName=TABLE_NAME)
        print(f"  table exists: {TABLE_NAME}")
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    print(f"  creating table: {TABLE_NAME}")
    ddb.create_table(
        TableName=TABLE_NAME,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "case_id", "AttributeType": "S"},
            {"AttributeName": "draft_id", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "case_id", "KeyType": "HASH"},
            {"AttributeName": "draft_id", "KeyType": "RANGE"},
        ],
        Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()],
    )
    ddb.get_waiter("table_exists").wait(TableName=TABLE_NAME)
    print("  table active")


def ensure_jobs_table(ddb) -> None:
    try:
        ddb.describe_table(TableName=JOBS_TABLE)
        print(f"  table exists: {JOBS_TABLE}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        print(f"  creating table: {JOBS_TABLE}")
        ddb.create_table(
            TableName=JOBS_TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()],
        )
        ddb.get_waiter("table_exists").wait(TableName=JOBS_TABLE)
        print("  table active")
    # TTL so job records self-clean.
    try:
        ddb.update_time_to_live(
            TableName=JOBS_TABLE,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": "expire_at"},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("ValidationException",):
            raise


def build_zip() -> bytes:
    """Package the sar_drafter source, handler, and sample case into a zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        pkg_dir = os.path.join(REPO_ROOT, "src", "sar_drafter")
        for root, _dirs, files in os.walk(pkg_dir):
            if "__pycache__" in root:
                continue
            for fn in files:
                if fn.endswith(".pyc"):
                    continue
                full = os.path.join(root, fn)
                arc = os.path.join("sar_drafter", os.path.relpath(full, pkg_dir))
                z.write(full, arc)
        z.write(os.path.join(REPO_ROOT, "deploy", "lambda_handler.py"), "lambda_handler.py")
        cases_dir = os.path.join(REPO_ROOT, "sample_data", "cases")
        for fn in sorted(os.listdir(cases_dir)):
            if fn.endswith(".json"):
                z.write(os.path.join(cases_dir, fn), os.path.join("sample_data", "cases", fn))
    return buf.getvalue()


def ensure_function(lam, role_arn: str, zip_bytes: bytes, model_id: str) -> None:
    env = {"Variables": {"SAR_TABLE": TABLE_NAME, "SAR_JOBS_TABLE": JOBS_TABLE, "SAR_MODEL_ID": model_id}}
    try:
        lam.get_function(FunctionName=FUNCTION_NAME)
        exists = True
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        exists = False

    if exists:
        print(f"  updating function: {FUNCTION_NAME}")
        lam.update_function_code(FunctionName=FUNCTION_NAME, ZipFile=zip_bytes)
        _wait_updated(lam)
        lam.update_function_configuration(
            FunctionName=FUNCTION_NAME,
            Role=role_arn,
            Handler=HANDLER,
            Runtime=RUNTIME,
            Timeout=300,
            MemorySize=512,
            Environment=env,
        )
        _wait_updated(lam)
        lam.tag_resource(
            Resource=lam.get_function(FunctionName=FUNCTION_NAME)["Configuration"]["FunctionArn"],
            Tags=TAGS,
        )
        return

    print(f"  creating function: {FUNCTION_NAME}")
    # Role propagation can lag; retry on the assume-role validation error.
    for attempt in range(1, 11):
        try:
            lam.create_function(
                FunctionName=FUNCTION_NAME,
                Runtime=RUNTIME,
                Role=role_arn,
                Handler=HANDLER,
                Code={"ZipFile": zip_bytes},
                Timeout=300,
                MemorySize=512,
                Environment=env,
                Description="Claude-powered AML/SAR investigation narrative drafter.",
                Tags=TAGS,
            )
            _wait_updated(lam)
            return
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("InvalidParameterValueException", "AccessDeniedException") and attempt < 10:
                print(f"    role not ready (attempt {attempt}); waiting...")
                time.sleep(6)
                continue
            raise


def _wait_updated(lam) -> None:
    try:
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION_NAME)
    except Exception:
        time.sleep(5)


def test_invoke(lam) -> int:
    print("\nInvoking with the bundled synthetic case...")
    resp = lam.invoke(FunctionName=FUNCTION_NAME, Payload=b"{}")
    payload = json.loads(resp["Payload"].read())
    if "errorMessage" in payload:
        print("  INVOKE ERROR:", payload.get("errorMessage"))
        print("  ", payload.get("errorType"))
        return 1
    print(f"  case_id              : {payload.get('case_id')}")
    print(f"  valid                : {payload.get('valid')}")
    print(f"  filing_recommendation: {payload.get('filing_recommendation')}")
    print(f"  rounds               : {payload.get('rounds')}")
    print(f"  storage              : {payload.get('storage')}")
    print(f"  model_id             : {payload.get('model_id')}")
    md = payload.get("sar_markdown") or ""
    print("\n----- SAR draft (first 900 chars) -----")
    print(md[:900])
    return 0 if payload.get("valid") else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Deploy the SAR drafter to AWS.")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--no-invoke", action="store_true")
    args = parser.parse_args(argv)

    session = boto3.session.Session(region_name=REGION)
    account_id = session.client("sts").get_caller_identity()["Account"]
    print(f"Deploying to account {account_id} / {REGION}\n")

    print("[1/4] IAM role")
    role_arn = ensure_role(session.client("iam"), account_id)

    print("[2/4] DynamoDB tables")
    ensure_table(session.client("dynamodb"))
    ensure_jobs_table(session.client("dynamodb"))

    print("[3/4] Package + Lambda function")
    zip_bytes = build_zip()
    print(f"  package size: {len(zip_bytes)/1024:.1f} KiB")
    ensure_function(session.client("lambda"), role_arn, zip_bytes, args.model_id)

    print("[4/4] Verify")
    rc = 0 if args.no_invoke else test_invoke(session.client("lambda"))

    print("\nDone. Re-run this script any time to update the deployment.")
    print(f"Invoke manually:\n  aws lambda invoke --function-name {FUNCTION_NAME} "
          f"--region {REGION} --payload '{{}}' out.json && cat out.json")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
