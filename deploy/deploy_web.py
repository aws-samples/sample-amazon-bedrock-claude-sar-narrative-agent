#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Expose the SAR drafter via CloudFront WITHOUT making the Lambda public.

Best-practice pattern:
  * Lambda Function URL with AuthType = AWS_IAM  (NOT world-accessible).
  * CloudFront Origin Access Control (OAC, origin type "lambda") signs every
    request to the Function URL with SigV4.
  * The Lambda resource policy grants lambda:InvokeFunctionUrl only to the
    CloudFront service principal, scoped to THIS distribution's ARN.

Anonymous callers hitting the Function URL directly get 403; only this
CloudFront distribution can invoke it. The API is asynchronous (see the
handler), so each request is fast and stays under CloudFront's 60s timeout.
"""

from __future__ import annotations

import time

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
FUNCTION_NAME = "sar-drafter"
DIST_COMMENT = "sar-drafter test UI"
OAC_NAME = "sar-drafter-oac"
TAGS = {"auto-delete": "no", "Project": "sar-drafter", "ManagedBy": "deploy_web.py"}

CACHE_POLICY_CACHING_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
ORIGIN_REQUEST_ALL_VIEWER_EXCEPT_HOST = "b689b0a8-53d0-40ab-baf2-68738e2966ac"


def ensure_function_url_iam(lam) -> str:
    """Create/point the Function URL at AWS_IAM auth (private)."""
    try:
        cfg = lam.get_function_url_config(FunctionName=FUNCTION_NAME)
        lam.update_function_url_config(FunctionName=FUNCTION_NAME, AuthType="AWS_IAM")
        url = cfg["FunctionUrl"]
        print(f"  function URL set to AWS_IAM: {url}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        url = lam.create_function_url_config(FunctionName=FUNCTION_NAME, AuthType="AWS_IAM")["FunctionUrl"]
        print(f"  created private (AWS_IAM) function URL: {url}")
    # Remove any legacy public-invoke permission.
    try:
        lam.remove_permission(FunctionName=FUNCTION_NAME, StatementId="AllowPublicFunctionUrl")
        print("  removed legacy public invoke permission")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    return url


def ensure_oac(cf) -> str:
    for oac in cf.list_origin_access_controls().get("OriginAccessControlList", {}).get("Items", []) or []:
        if oac.get("Name") == OAC_NAME:
            print(f"  OAC exists: {oac['Id']}")
            return oac["Id"]
    resp = cf.create_origin_access_control(OriginAccessControlConfig={
        "Name": OAC_NAME,
        "Description": "Sign CloudFront->Lambda Function URL requests",
        "SigningProtocol": "sigv4",
        "SigningBehavior": "always",
        "OriginAccessControlOriginType": "lambda",
    })
    oac_id = resp["OriginAccessControl"]["Id"]
    print(f"  created OAC: {oac_id}")
    return oac_id


def _origin_host(function_url: str) -> str:
    return function_url.replace("https://", "").replace("http://", "").rstrip("/")


def _find_distribution(cf):
    for page in cf.get_paginator("list_distributions").paginate():
        for item in (page.get("DistributionList", {}) or {}).get("Items", []) or []:
            if item.get("Comment") == DIST_COMMENT:
                return item["Id"], item["DomainName"]
    return None, None


def ensure_distribution(cf, origin_host: str, oac_id: str):
    dist_id, domain = _find_distribution(cf)
    if dist_id:
        print(f"  distribution exists: https://{domain} ({dist_id})")
        return dist_id, domain

    origin_id = "sar-drafter-func-url"
    config = {
        "CallerReference": f"sar-drafter-{int(time.time())}",
        "Comment": DIST_COMMENT,
        "Enabled": True,
        "Aliases": {"Quantity": 0},
        "DefaultRootObject": "",
        "Origins": {"Quantity": 1, "Items": [{
            "Id": origin_id,
            "DomainName": origin_host,
            "OriginAccessControlId": oac_id,
            "CustomHeaders": {"Quantity": 0},
            "CustomOriginConfig": {
                "HTTPPort": 80, "HTTPSPort": 443,
                "OriginProtocolPolicy": "https-only",
                "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
                "OriginReadTimeout": 60, "OriginKeepaliveTimeout": 5,
            },
        }]},
        "OriginGroups": {"Quantity": 0},
        "DefaultCacheBehavior": {
            "TargetOriginId": origin_id,
            "ViewerProtocolPolicy": "redirect-to-https",
            "Compress": True,
            "AllowedMethods": {
                "Quantity": 7,
                "Items": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
                "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
            },
            "CachePolicyId": CACHE_POLICY_CACHING_DISABLED,
            "OriginRequestPolicyId": ORIGIN_REQUEST_ALL_VIEWER_EXCEPT_HOST,
            "SmoothStreaming": False,
            "FieldLevelEncryptionId": "",
            "LambdaFunctionAssociations": {"Quantity": 0},
            "FunctionAssociations": {"Quantity": 0},
            "TrustedSigners": {"Enabled": False, "Quantity": 0},
            "TrustedKeyGroups": {"Enabled": False, "Quantity": 0},
        },
        "CacheBehaviors": {"Quantity": 0},
        "CustomErrorResponses": {"Quantity": 0},
        "Logging": {"Enabled": False, "IncludeCookies": False, "Bucket": "", "Prefix": ""},
        "PriceClass": "PriceClass_100",
        "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
        "Restrictions": {"GeoRestriction": {"RestrictionType": "none", "Quantity": 0}},
        "WebACLId": "",
        "HttpVersion": "http2",
        "IsIPV6Enabled": True,
    }
    resp = cf.create_distribution_with_tags(DistributionConfigWithTags={
        "DistributionConfig": config,
        "Tags": {"Items": [{"Key": k, "Value": v} for k, v in TAGS.items()]},
    })
    dist_id = resp["Distribution"]["Id"]
    domain = resp["Distribution"]["DomainName"]
    print(f"  created distribution: https://{domain} ({dist_id}, status {resp['Distribution']['Status']})")
    return dist_id, domain


def ensure_cf_invoke_permission(lam, dist_arn: str) -> None:
    try:
        lam.add_permission(
            FunctionName=FUNCTION_NAME,
            StatementId="AllowCloudFrontOAC",
            Action="lambda:InvokeFunctionUrl",
            Principal="cloudfront.amazonaws.com",
            SourceArn=dist_arn,
        )
        print("  granted CloudFront (scoped to this distribution) invoke permission")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceConflictException":
            raise
        print("  CloudFront invoke permission already present")


def main() -> int:
    session = boto3.session.Session(region_name=REGION)
    account_id = session.client("sts").get_caller_identity()["Account"]
    lam = session.client("lambda")
    cf = session.client("cloudfront")

    print("[1/4] Private Function URL (AWS_IAM)")
    function_url = ensure_function_url_iam(lam)

    print("[2/4] CloudFront Origin Access Control")
    oac_id = ensure_oac(cf)

    print("[3/4] CloudFront distribution")
    dist_id, domain = ensure_distribution(cf, _origin_host(function_url), oac_id)

    print("[4/4] Scoped Lambda resource policy")
    dist_arn = f"arn:aws:cloudfront::{account_id}:distribution/{dist_id}"
    ensure_cf_invoke_permission(lam, dist_arn)

    print("\n" + "=" * 64)
    print(f"CloudFront URL : https://{domain}")
    print("Lambda is PRIVATE (AuthType=AWS_IAM); only this distribution can invoke it.")
    print("=" * 64)
    print("CloudFront takes 5-15 minutes to finish deploying globally.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
