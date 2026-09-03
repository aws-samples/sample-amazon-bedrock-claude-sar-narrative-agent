# Security

This document describes the security posture of the AML SAR Investigation
Drafter sample. It is written to support a pre-publication security review.

## What this is (and is not)

- A **sample / reference implementation** that shows how to build a grounded,
  human-in-the-loop document-drafting agent on Claude via Amazon Bedrock.
- It is **decision-support** software: it produces a *draft* for a qualified
  analyst to review and file. It does not file reports and does not take any
  action in the world.
- It is **not** a production compliance system. Sections below call out what a
  production deployment must add.

## Reporting a vulnerability

Please do **not** open a public GitHub issue for security problems. Report
suspected vulnerabilities privately to the repository maintainers (or, for AWS
sample repositories, via the AWS vulnerability reporting process at
<https://aws.amazon.com/security/vulnerability-reporting/>). We will acknowledge
and work the report privately before any public disclosure.

## Data classification

- **All bundled data is synthetic.** The cases under `sample_data/` are
  fabricated (invented names, accounts, and transactions). They contain no PII,
  no customer data, and no business data.
- The demo must be exercised with synthetic data only. Do **not** submit real
  customer, financial, or personal data to the sample deployment.

## Deployment security model

The optional AWS deployment follows least-privilege and "no world-accessible
compute" principles.

### Network / access boundary
- The Lambda **Function URL uses `AuthType = AWS_IAM`** — it is not publicly
  invocable. Anonymous requests receive HTTP 403.
- **CloudFront Origin Access Control (OAC)** signs every origin request (SigV4).
  The Lambda **resource policy** grants `lambda:InvokeFunctionUrl` only to the
  CloudFront service principal, scoped by `AWS:SourceArn` to the specific
  distribution. The EventBridge trigger is likewise scoped to a specific rule ARN.
- This satisfies AWS Security Hub control **CloudFront.16** (use OAC for Lambda
  function URL origins).
- All viewer traffic is HTTPS (CloudFront `redirect-to-https`).

### Application authentication
- A cookie-session **sign-in page** gates the UI and all API routes.
- The session cookie is **HMAC-signed** (SHA-256) and set `HttpOnly`, `Secure`,
  `SameSite=Lax`, with an 8-hour expiry. It carries no sensitive data beyond a
  username and expiry.
- Credentials are provided as Lambda environment variables: `SAR_AUTH_USER`, a
  **SHA-256 hash** of the password (`SAR_AUTH_PASSWORD_SHA256`, not the plaintext),
  and a signing secret (`SAR_AUTH_SECRET`).
- **Demo-grade, by design.** For production, replace this with managed identity
  (Amazon Cognito / OIDC), store the signing secret in AWS Secrets Manager or
  SSM Parameter Store (SecureString), and add rate limiting / AWS WAF.

### Least-privilege IAM
The Lambda execution role grants only:
- `bedrock:InvokeModel` / `InvokeModelWithResponseStream` on Anthropic
  foundation models and inference profiles (for the drafting call);
- `dynamodb:PutItem/GetItem/Query/UpdateItem` on the two project tables only;
- `s3:GetObject` on the project's cases bucket only (for the S3 ingest path);
- `lambda:InvokeFunction` on **itself** only (async job worker);
- CloudWatch Logs via the AWS managed basic-execution policy.
No wildcard resource grants beyond the Bedrock model/profile ARNs (which are
account/region agnostic by design for cross-region inference).

### Data at rest / in transit
- DynamoDB uses AWS-owned key encryption at rest by default; the jobs table
  carries a TTL so records self-expire. S3 objects use SSE by default.
- For production, use a customer-managed KMS key (CMK) on the tables, bucket,
  and any logs, and enable point-in-time recovery as appropriate.

## Input handling and responsible AI

- **Case data is untrusted input treated as data, not instructions.** A case
  submitted via the API or dropped in S3 can only influence the *text* of a
  draft, which a human reviews. It cannot cause the agent to take actions:
  every tool is **read-only** over the supplied case, and there are no
  state-changing or outbound tools.
- **Prompt-injection containment:** because the tools cannot act and the output
  is a reviewed draft, prompt injection in a case narrative is limited to
  influencing wording. The output is additionally constrained by a **strict
  JSON schema** and a **citation-grounding check** (every cited transaction ID
  must exist in the case; fabricated IDs fail validation and the eval).
- **Human-in-the-loop is mandatory.** The system recommends
  `recommend_file` / `needs_human_review` / `recommend_no_file`; a person makes
  the filing decision. This is enforced in the system prompt and the framing.
- **No autonomous remediation or filing** of any kind.

## Secrets

- No secrets are committed to the repository. Verified by scan (no access keys,
  private keys, or hardcoded passwords).
- Runtime secrets (auth signing secret, password hash) live in Lambda
  environment variables for the demo; move them to Secrets Manager / SSM for
  production.
- Do not paste real credentials into `sample_data/` or documentation.

## Cost / abuse considerations

- Each draft triggers Bedrock model calls, which incur cost. The deployment is
  authenticated; for any shared/public exposure add AWS WAF rate limiting and
  monitor Bedrock usage and CloudWatch alarms.

## Dependencies / supply chain

- The core library and offline tests/eval require **no third-party packages**
  (Python standard library only); the bundled mock provider runs offline.
- Optional extras: `boto3` (provided by the Lambda runtime) for Bedrock and AWS
  deployment, and `anthropic` for the direct-API provider. Versions are pinned
  with minimums in `requirements.txt` / `pyproject.toml`.

## Teardown

`python deploy/teardown.py` removes every resource this project creates
(Lambda, tables, role, bucket, EventBridge rule, Function URL, CloudFront
distribution, and the sign-in artifacts), leaving no residual access.
