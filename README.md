# AML SAR Investigation Narrative Drafter

An agent that investigates an anti-money-laundering (AML) case and drafts an
evidence-cited, regulator-ready **Suspicious Activity Report (SAR)** narrative
for a human analyst to review and file.

Powered by **Claude** (on Amazon Bedrock, or the Anthropic API). Runs fully
**offline** out of the box with a bundled mock provider, so you can try it with
no credentials.

> **Human-in-the-loop, always.** This tool produces a *draft* for review. It
> does not file SARs, does not contact subjects, and only reads case data. A
> qualified BSA/AML analyst verifies every fact and makes the filing decision.

---

## Why this problem, and why Claude is the engine

Writing SAR narratives is one of the highest-volume, highest-stakes chores in a
bank's financial-crime unit. Analysts spend hours per case turning raw
transaction data into a clear, defensible narrative that covers *who, what,
when, where, and why/how* the activity is suspicious. It is tedious, quality
varies by analyst, and the narrative is the mandatory deliverable a regulator
reads.

There is no upstream system that writes this narrative. Remove the model and you
have nothing but raw alerts. That is what makes this a genuine Claude use case
rather than a wrapper:

- **Reasoning over evidence** - correlating deposits, wires, KYC, and alerts into
  a coherent story is the whole job.
- **Evidence discipline** - every claim in the draft is tied to specific
  transaction IDs the agent actually retrieved. The eval enforces zero
  fabricated citations.
- **Honest hedging** - when the evidence is ambiguous, the agent recommends
  `needs_human_review` and lists what is missing, instead of forcing a call.
- **Regulator-ready form** - the FinCEN five-element narrative structure.

## Architecture

![AML SAR Investigation Drafter reference architecture](docs/architecture.svg)

Claude on Amazon Bedrock is the engine; the surrounding services keep it private
(CloudFront + Origin Access Control in front of an IAM-auth Lambda), asynchronous
(so long investigations stay under CloudFront's 60s timeout), and event-driven
(drop a case in S3 and it auto-drafts). See [SECURITY.md](SECURITY.md) for the
full security model.

## How it works

```
                 ┌─────────────────────────────────────────────┐
   case JSON ──▶ │  agent loop (bounded tool-use conversation)  │
                 │                                              │
                 │   Claude ⇄ read-only investigation tools     │
                 │     get_case_overview / get_subject_profile  │
                 │     get_account_activity / get_transactions  │
                 │     get_alerts / get_related_parties /       │
                 │     get_prior_sars / lookup_watchlist        │
                 │                                              │
                 │   ↳ submit_sar  →  schema validation         │
                 └─────────────────────────────────────────────┘
                                     │
                                     ▼
                   validated SAR draft  →  markdown / JSON
```

The agent investigates first (it must pull the data before drawing
conclusions), then calls `submit_sar` with a structured draft. The loop
validates that draft against a strict schema and returns any errors to the model
to fix, within a bounded round budget. Tools are strictly **read-only** - the
safety boundary for a defensive workflow.

## Quickstart

No dependencies are needed for the offline run.

```bash
# 1) Offline, deterministic, no credentials (mock provider):
python run.py --provider mock --show-trace

# 2) Claude on Amazon Bedrock (needs boto3 + AWS creds with Bedrock access):
pip install -r requirements.txt
python run.py --provider bedrock --region us-east-1

# 3) Claude via the Anthropic API:
pip install -r requirements.txt
ANTHROPIC_API_KEY=sk-... python run.py --provider anthropic
```

Useful flags: `--json` (raw SAR JSON), `--out draft.md` (write to file),
`--case path/to/case.json` (your own case), `--model <id>`, `--max-rounds N`.

## Evaluation

The eval harness is what makes this a serious sample. It scores each draft on
the qualities that matter for a compliance artifact - above all **citation
grounding** (no fabricated transaction IDs).

```bash
python eval/run_eval.py                       # offline (mock)
python eval/run_eval.py --provider bedrock    # against Claude on Bedrock
```

Metrics: schema validity, citation grounding + hallucinated-ID count, typology
recall, subject coverage, recommendation match, activity-period sanity, and
amount floor. A case passes only when it is schema-valid, fully grounded, recalls
the expected typologies, and matches the expected recommendation.

## Tests

```bash
python tests/test_sar_drafter.py     # or: python -m unittest discover -s tests
```

## Deploy to AWS

Deploy the agent as a Lambda function backed by Claude on Bedrock, with a
DynamoDB table for drafts. The deploy is idempotent and re-runnable, and tags
every resource `auto-delete: no`.

```bash
pip install boto3
python deploy/deploy.py                     # role + table + function + test invoke
python deploy/deploy.py --model-id us.anthropic.claude-sonnet-4-5-20250929-v1:0
```

It provisions:
- IAM role `sar-drafter-lambda-role` (scoped: Bedrock invoke on Anthropic
  models + inference profiles, DynamoDB write, CloudWatch Logs);
- DynamoDB table `sar-drafts` (PAY_PER_REQUEST);
- Lambda function `sar-drafter` (Python 3.12; boto3 from the runtime, no bundled deps).

Invoke it:
```bash
# bundled synthetic case:
aws lambda invoke --function-name sar-drafter --payload '{}' \
  --cli-binary-format raw-in-base64-out out.json && cat out.json

# your own case inline: {"case": { ...case JSON... }}
```

Your account must have access to the chosen Claude model on Bedrock. This
account uses cross-region inference profiles (e.g.
`us.anthropic.claude-sonnet-4-5-20250929-v1:0`); pass a different one with
`--model-id` if needed.

### Web test UI (CloudFront + private Lambda)

```bash
python deploy/deploy_web.py       # private Function URL + CloudFront OAC + distribution
```

Serves a browser test page from CloudFront **without exposing the Lambda**:

- The Function URL uses `AuthType = AWS_IAM` (not world-accessible; anonymous
  calls get 403).
- CloudFront **Origin Access Control (OAC)** signs every request to the
  Function URL with SigV4.
- The Lambda resource policy grants invoke only to the CloudFront service
  principal, scoped to *this distribution's ARN*.

Because a full investigation can exceed CloudFront's 60s origin timeout, the API
is **asynchronous**: `POST /draft` returns a `job_id` immediately, a background
worker drafts, and the page polls `GET /result?job_id=...`. Each hop is
sub-second.

> **OAC + POST note:** CloudFront can't read a request body, so for `POST` the
> caller must send an `x-amz-content-sha256` header (the body's SHA-256) for the
> OAC signature to validate. The bundled UI computes this in the browser via
> `crypto.subtle`. This is why the UI must be served over HTTPS (it is, via
> CloudFront).

This satisfies the "CloudFront yes, world-accessible Lambda no" requirement and
the AWS Security Hub control *[CloudFront.16] distributions should use OAC for
Lambda function URL origins*.

#### Sign-in page (cookie session)

```bash
python deploy/deploy_auth.py --user analyst              # random password, printed once
python deploy/deploy_auth.py --user demo --password <password>
python deploy/deploy_auth.py --remove                   # disable the login gate
```

Access is gated by a proper **branded sign-in page** served by the Lambda. On
successful login it issues an **HMAC-signed, HttpOnly, Secure session cookie**
(8-hour TTL); the UI and all API routes (`/cases`, `/draft`, `/result`) require
a valid session, and there's a Sign-out link.

This works because the Lambda *can* read the `Cookie` header (OAC forwards it),
even though it cannot read `Authorization` (OAC replaces it with its SigV4
signature). Credentials are set as Lambda env vars (`SAR_AUTH_USER`, a SHA-256
of the password, and a signing secret). Fine for gating a demo; use Cognito/OIDC
for production identity.

> Re-running `deploy/deploy.py` resets the function's env vars, so re-run
> `deploy/deploy_auth.py` afterward to restore the login credentials.

### Event-driven auto-draft (S3 + EventBridge)

```bash
python deploy/deploy_events.py    # cases bucket + EventBridge rule -> Lambda
# then drop a case and it drafts automatically:
aws s3 cp sample_data/cases/case_001_structuring.json \
  s3://sar-cases-<account>/incoming/case_001.json
```

Uploading a `*.json` case to the bucket triggers the drafter; the result is
stored in the `sar-drafts` DynamoDB table.

## Project structure

```
sar-narrative-drafter/
├── run.py                       # convenience launcher (no PYTHONPATH needed)
├── requirements.txt             # all optional; core runs offline
├── src/sar_drafter/
│   ├── schema.py                # SAR schema + dependency-free validation
│   ├── prompts.py               # investigator system prompt / playbook
│   ├── tools.py                 # read-only investigation tools + specs
│   ├── agent.py                 # bounded tool-use agent loop
│   ├── render.py                # SAR draft → markdown (+ disclaimer)
│   ├── cli.py                   # command-line entrypoint
│   └── providers/               # bedrock / anthropic / mock
├── deploy/                      # deploy.py, deploy_web.py, deploy_events.py,
│                                #   teardown.py, teardown_web.py, lambda_handler.py
├── sample_data/cases/           # synthetic AML cases (no real PII)
├── eval/                        # labeled cases + scoring harness
├── tests/
└── docs/architecture.svg        # reference architecture diagram
```

## Web UI

The web UI (served privately via CloudFront) offers a case picker for the three
sample cases, a tabbed report (Report / Evidence / Investigation / Raw), a
grounding badge (verified vs. fabricated transactions), a confidence bar, the
investigation trace, and Markdown/JSON downloads.

## Sample data

All case data is **synthetic** and contains no real people or PII. The bundled
case models a classic pattern: sub-threshold cash deposits (structuring) across
two branches, rapidly wired out to a new beneficiary in a high-risk jurisdiction.

## Roadmap (Phase 0 → AWS)

Phase 0 (this repo) is a fully runnable local agent. Planned phases:

- **Phase 1** - real data adapters (case data from a datastore; watchlist from a
  sanctions/PEP screening provider).
- **Phase 2** - event-driven deployment on AWS (EventBridge → Lambda / Step
  Functions, DynamoDB for drafts, review UI). All infrastructure tagged
  `auto-delete: no`.
- **Phase 3** - reviewer workflow, feedback capture, and an expanded labeled
  benchmark for regression tracking.

The same engine is designed to drive sibling "drafters" (credit memo,
regulatory change-impact) by swapping the playbook and tools.

## Responsible use

This is decision-support software for licensed institutions operating on their
own data. It must not be used to evade detection, and it never replaces the
independent judgment of a qualified analyst or the institution's filing
obligations. Verify every fact against source systems before filing.

## Security

See [SECURITY.md](SECURITY.md) for the security model, vulnerability reporting,
data classification (synthetic only — no PII), least-privilege IAM, and the
responsible-AI / prompt-injection posture.

## License

MIT-0. See [LICENSE](LICENSE).
