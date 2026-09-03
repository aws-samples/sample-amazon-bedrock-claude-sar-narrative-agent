# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AWS Lambda handler for the SAR drafter (web UI + async API + S3 ingest).

Runs privately behind CloudFront (OAC) with the Function URL set to
AuthType=AWS_IAM - the function is NOT world-accessible.

HTTP routes (async, to stay under CloudFront's 60s origin timeout):
  GET  /                    -> the browser test UI
  GET  /cases               -> list bundled sample cases (metadata)
  POST /draft               -> create a job {case_key|case}; return {job_id}
  GET  /result?job_id=...   -> poll job status/result
  (async worker)  {"job": {...}} -> run the drafter, store the result

Also: S3 upload via EventBridge, and direct invoke for CLI/tests.
boto3 is provided by the Lambda runtime; no extra dependencies are bundled.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import time
import uuid
from decimal import Decimal

from sar_drafter.agent import run_investigation
from sar_drafter.providers import get_provider
from sar_drafter.render import render_sar_markdown
from sar_drafter.schema import all_cited_txn_ids
from sar_drafter.tools import CaseData, default_case_path

DEFAULT_MODEL_ID = os.environ.get("SAR_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
JOBS_TABLE = os.environ.get("SAR_JOBS_TABLE")
MAX_BODY_BYTES = 512 * 1024

# --- authentication (cookie session) --------------------------------------
AUTH_USER = os.environ.get("SAR_AUTH_USER")
AUTH_PW_SHA256 = os.environ.get("SAR_AUTH_PASSWORD_SHA256")
AUTH_SECRET = os.environ.get("SAR_AUTH_SECRET")
AUTH_ENABLED = bool(AUTH_USER and AUTH_PW_SHA256 and AUTH_SECRET)
COOKIE_NAME = "sar_session"
SESSION_TTL = 8 * 3600


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _sign(payload_b64: str) -> str:
    return hmac.new(AUTH_SECRET.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()


def _make_token(user: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"u": user, "exp": int(time.time()) + SESSION_TTL}).encode("utf-8")
    ).decode("utf-8")
    return payload + "." + _sign(payload)


def _valid_token(tok: str) -> bool:
    try:
        payload, sig = tok.split(".", 1)
        if not hmac.compare_digest(sig, _sign(payload)):
            return False
        data = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
        return int(data.get("exp", 0)) > int(time.time())
    except Exception:
        return False


def _cookies(event: dict) -> dict:
    out = {}
    for c in event.get("cookies", []) or []:
        if "=" in c:
            k, v = c.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _is_authed(event: dict) -> bool:
    if not AUTH_ENABLED:
        return True
    return _valid_token(_cookies(event).get(COOKIE_NAME, ""))


# --- sample case discovery ------------------------------------------------

def _cases_dir() -> str:
    return os.path.dirname(default_case_path())


def _allowed_case_keys() -> dict:
    d = _cases_dir()
    out = {}
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".json"):
                out[fn[:-5]] = os.path.join(d, fn)
    return out


def _case_meta(key: str, path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        c = json.load(f)
    subjects = c.get("subjects", [])
    return {
        "key": key,
        "case_id": c.get("case_id"),
        "priority": c.get("priority"),
        "title": subjects[0]["name"] if subjects else key,
        "subject_count": len(subjects),
        "txn_count": len(c.get("transactions", [])),
        "alert_count": len(c.get("alerts", [])),
        "blurb": (c.get("analyst_notes") or "")[:180],
    }


def _list_cases() -> list:
    return [_case_meta(k, p) for k, p in _allowed_case_keys().items()]


def _load_case_by_key(key: str) -> dict:
    allowed = _allowed_case_keys()
    if key not in allowed:
        raise ValueError(f"unknown case_key '{key}'")
    with open(allowed[key], "r", encoding="utf-8") as f:
        return json.load(f)


# --- core drafting --------------------------------------------------------

def _run(case_dict, model_id: str, store: bool):
    region = os.environ.get("AWS_REGION", "us-east-1")
    case = CaseData(case_dict) if isinstance(case_dict, dict) and case_dict else CaseData.from_file(default_case_path())
    provider = get_provider("bedrock", model_id=model_id, region_name=region)

    t0 = time.time()
    result = run_investigation(case, provider)
    elapsed = int(time.time() - t0)

    sar = result.sar or {}
    sar_markdown = render_sar_markdown(result.sar, result.case_id) if result.sar else None

    case_ids = set(case.transaction_ids())
    cited = list(dict.fromkeys(all_cited_txn_ids(sar)))
    verified = [c for c in cited if c in case_ids]
    hallucinated = [c for c in cited if c not in case_ids]
    grounding = {"cited": len(cited), "verified": len(verified), "hallucinated": hallucinated}

    # Trim the trace to what the UI shows.
    trace = [{"tool": s.get("tool"), "input": s.get("input", {})} for s in result.trace]

    storage = _store_draft(result, sar_markdown or "") if store else "not stored"
    return {
        "case_id": result.case_id,
        "valid": result.valid,
        "finished_reason": result.finished_reason,
        "rounds": result.rounds,
        "elapsed_seconds": elapsed,
        "model_id": model_id,
        "filing_recommendation": sar.get("filing_recommendation"),
        "confidence": sar.get("confidence"),
        "sar": sar,
        "sar_markdown": sar_markdown,
        "trace": trace,
        "grounding": grounding,
        "storage": storage,
        "errors": result.errors,
    }


def _store_draft(result, sar_markdown: str) -> str:
    table_name = os.environ.get("SAR_TABLE")
    if not table_name:
        return "skipped (no SAR_TABLE configured)"
    try:
        import boto3

        table = boto3.resource("dynamodb").Table(table_name)
        draft_id = _dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S%fZ")
        table.put_item(Item={
            "case_id": result.case_id, "draft_id": draft_id, "created_at": draft_id,
            "filing_recommendation": (result.sar or {}).get("filing_recommendation", "unknown"),
            "valid": result.valid, "sar_json": json.dumps(result.sar or {}), "sar_markdown": sar_markdown,
        })
        return f"stored draft_id={draft_id}"
    except Exception as exc:  # pragma: no cover
        return f"store failed: {exc}"


# --- async jobs -----------------------------------------------------------

def _jobs_table():
    import boto3

    return boto3.resource("dynamodb").Table(JOBS_TABLE)


def _put_job(job_id: str, status: str, extra: dict = None) -> None:
    if not JOBS_TABLE:
        return
    item = {"job_id": job_id, "status": status,
            "updated_at": _dt.datetime.utcnow().isoformat() + "Z",
            "expire_at": int(time.time()) + 86400}
    item.update(extra or {})
    _jobs_table().put_item(Item=item)


def _get_job(job_id: str) -> dict:
    if not JOBS_TABLE:
        return {}
    return _jobs_table().get_item(Key={"job_id": job_id}).get("Item", {})


def _run_job(job: dict) -> dict:
    job_id = job.get("job_id")
    try:
        r = _run(job.get("case"), job.get("model_id", DEFAULT_MODEL_ID), store=True)
        _put_job(job_id, "done", {
            "case_id": r.get("case_id"), "valid": r.get("valid"),
            "filing_recommendation": r.get("filing_recommendation"),
            "confidence": json.dumps(r.get("confidence")),
            "rounds": r.get("rounds"), "elapsed_seconds": r.get("elapsed_seconds"),
            "model_id": r.get("model_id"),
            "sar_json": json.dumps(r.get("sar") or {}),
            "sar_markdown": r.get("sar_markdown") or "",
            "trace_json": json.dumps(r.get("trace") or []),
            "grounding_json": json.dumps(r.get("grounding") or {}),
        })
        return {"job_id": job_id, "status": "done"}
    except Exception as exc:  # pragma: no cover
        _put_job(job_id, "error", {"error": str(exc)})
        return {"job_id": job_id, "status": "error", "error": str(exc)}


def _start_job(case_dict, model_id: str, case_id_hint: str) -> str:
    import boto3

    job_id = uuid.uuid4().hex
    _put_job(job_id, "pending", {"case_id": case_id_hint})
    boto3.client("lambda").invoke(
        FunctionName=os.environ["AWS_LAMBDA_FUNCTION_NAME"],
        InvocationType="Event",
        Payload=json.dumps({"job": {"job_id": job_id, "case": case_dict, "model_id": model_id}}).encode("utf-8"),
    )
    return job_id


# --- HTTP -----------------------------------------------------------------

def _json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o % 1 == 0 else float(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def _json_response(status: int, obj) -> dict:
    return {"statusCode": status,
            "headers": {"content-type": "application/json", "cache-control": "no-store"},
            "body": json.dumps(obj, default=_json_default)}


def _html_response(html: str) -> dict:
    return {"statusCode": 200,
            "headers": {"content-type": "text/html; charset=utf-8", "cache-control": "no-store"},
            "body": html}


def _read_json_body(event: dict):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        return None, _json_response(413, {"error": "request body too large"})
    try:
        return (json.loads(raw) if raw.strip() else {}), None
    except ValueError:
        return None, _json_response(400, {"error": "invalid JSON body"})


def _login_response(user: str) -> dict:
    cookie = (f"{COOKIE_NAME}={_make_token(user)}; HttpOnly; Secure; SameSite=Lax; "
              f"Path=/; Max-Age={SESSION_TTL}")
    return {"statusCode": 200,
            "headers": {"content-type": "application/json", "cache-control": "no-store",
                        "set-cookie": cookie},
            "body": json.dumps({"ok": True})}


def _handle_http(event: dict) -> dict:
    http = event.get("requestContext", {}).get("http", {})
    method = (http.get("method") or "GET").upper()
    path = event.get("rawPath") or "/"

    # --- auth routes (always available) ---
    if method == "POST" and path.endswith("/login"):
        data, err = _read_json_body(event)
        if err:
            return err
        user = (data or {}).get("username", "")
        pw = (data or {}).get("password", "")
        if AUTH_ENABLED and hmac.compare_digest(user, AUTH_USER) and \
                hmac.compare_digest(_sha256_hex(pw), AUTH_PW_SHA256):
            return _login_response(user)
        return _json_response(401, {"error": "invalid username or password"})

    if method == "GET" and path.endswith("/logout"):
        clear = f"{COOKIE_NAME}=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0"
        return {"statusCode": 302, "headers": {"location": "/", "set-cookie": clear, "cache-control": "no-store"}, "body": ""}

    authed = _is_authed(event)

    if method == "GET" and path in ("/", ""):
        return _html_response(INDEX_HTML if authed else LOGIN_HTML)

    # --- everything below requires a session ---
    if not authed:
        return _json_response(401, {"error": "authentication required"})

    if method == "GET" and path.endswith("/cases"):
        try:
            return _json_response(200, {"cases": _list_cases()})
        except Exception as exc:  # pragma: no cover
            return _json_response(500, {"error": str(exc)})

    if method == "GET" and path.endswith("/result"):
        job_id = (event.get("queryStringParameters") or {}).get("job_id", "")
        if not job_id:
            return _json_response(400, {"error": "missing job_id"})
        job = _get_job(job_id)
        if not job:
            return _json_response(404, {"error": "unknown job_id"})
        out = {"job_id": job_id, "status": job.get("status")}
        if job.get("status") == "done":
            out.update({
                "case_id": job.get("case_id"), "valid": job.get("valid"),
                "filing_recommendation": job.get("filing_recommendation"),
                "confidence": json.loads(job.get("confidence", "null")),
                "rounds": job.get("rounds"), "elapsed_seconds": job.get("elapsed_seconds"),
                "model_id": job.get("model_id"),
                "sar": json.loads(job.get("sar_json", "{}")),
                "sar_markdown": job.get("sar_markdown"),
                "trace": json.loads(job.get("trace_json", "[]")),
                "grounding": json.loads(job.get("grounding_json", "{}")),
            })
        elif job.get("status") == "error":
            out["error"] = job.get("error")
        return _json_response(200, out)

    if method == "POST" and path.endswith("/draft"):
        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8", "replace")
        if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
            return _json_response(413, {"error": "request body too large"})
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return _json_response(400, {"error": "invalid JSON body"})

        case_dict = None
        hint = "bundled-sample"
        try:
            if data.get("case_key"):
                case_dict = _load_case_by_key(data["case_key"])
                hint = case_dict.get("case_id", data["case_key"])
            elif isinstance(data.get("case"), dict):
                case_dict = data["case"]
                hint = case_dict.get("case_id", "custom")
        except ValueError as exc:
            return _json_response(400, {"error": str(exc)})

        try:
            job_id = _start_job(case_dict, data.get("model_id", DEFAULT_MODEL_ID), hint)
        except Exception as exc:  # pragma: no cover
            return _json_response(500, {"error": str(exc)})
        return _json_response(202, {"job_id": job_id, "status": "pending"})

    return _json_response(404, {"error": f"no route for {method} {path}"})


# --- S3 / EventBridge -----------------------------------------------------

def _handle_s3(event: dict) -> dict:
    import boto3

    detail = event.get("detail") or {}
    if detail.get("bucket") and detail.get("object"):
        bucket, key = detail["bucket"]["name"], detail["object"]["key"]
    elif event.get("Records"):
        rec = event["Records"][0]["s3"]
        bucket, key = rec["bucket"]["name"], rec["object"]["key"]
    else:
        return {"error": "unrecognized S3 event"}
    if not key.lower().endswith(".json"):
        return {"skipped": f"not a .json object: {key}"}
    case_dict = json.loads(boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read())
    r = _run(case_dict, DEFAULT_MODEL_ID, store=True)
    return {"source": f"s3://{bucket}/{key}", **{k: r[k] for k in ("case_id", "valid", "filing_recommendation", "storage")}}


# --- dispatch -------------------------------------------------------------

def handler(event, context):
    event = event or {}
    if isinstance(event.get("job"), dict):
        return _run_job(event["job"])
    rc = event.get("requestContext")
    if isinstance(rc, dict) and "http" in rc:
        return _handle_http(event)
    if event.get("detail-type") == "Object Created" or (isinstance(event.get("detail"), dict) and event["detail"].get("bucket")):
        return _handle_s3(event)
    if event.get("Records"):
        return _handle_s3(event)
    return _run(event.get("case"), event.get("model_id", DEFAULT_MODEL_ID), store=bool(event.get("store", True)))


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>AML SAR Investigation Copilot</title>
<style>
  :root{
    --bg:#0f1117; --panel:#171a23; --panel2:#1e222d; --line:#2a2f3c;
    --text:#e7e9ee; --muted:#9aa3b2; --accent:#7c5cff; --accent2:#d97757;
    --aws:#20b2aa; --file:#ff6b6b; --review:#f4b740; --nofile:#3ecf8e;
    --shadow:0 10px 30px rgba(0,0,0,.35);
  }
  @media (prefers-color-scheme: light){
    :root{ --bg:#f4f6fb; --panel:#ffffff; --panel2:#f0f2f8; --line:#e2e6ef;
           --text:#1b2030; --muted:#5b6473; --shadow:0 8px 24px rgba(20,30,60,.08); }
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
       background:var(--bg);color:var(--text);line-height:1.5}
  .wrap{max-width:1080px;margin:0 auto;padding:0 20px 60px}
  header.hero{background:linear-gradient(120deg,#5b3fd6 0%,#7c5cff 45%,#d97757 120%);
       color:#fff;padding:34px 0 30px;box-shadow:var(--shadow)}
  .hero .wrap{display:flex;align-items:center;justify-content:space-between;gap:20px;flex-wrap:wrap}
  .hero h1{margin:0;font-size:1.7rem;letter-spacing:-.3px}
  .hero p{margin:6px 0 0;opacity:.92;font-size:.98rem}
  .pill{display:inline-block;background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.3);
        padding:6px 12px;border-radius:999px;font-size:.8rem;font-weight:600;backdrop-filter:blur(4px)}
  .disclaimer{background:#3a2d12;border:1px solid #6b5320;color:#f4d58a;border-radius:10px;
       padding:10px 14px;font-size:.86rem;margin:18px 0}
  @media (prefers-color-scheme: light){ .disclaimer{background:#fff6e0;border-color:#ffdf9e;color:#7a5a00} }
  h2.sec{font-size:.82rem;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);margin:26px 0 12px}
  .cases{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;
        box-shadow:var(--shadow);display:flex;flex-direction:column;gap:8px;transition:transform .12s}
  .card:hover{transform:translateY(-2px)}
  .card h3{margin:0;font-size:1.02rem}
  .card .meta{color:var(--muted);font-size:.82rem}
  .card .blurb{font-size:.86rem;color:var(--muted);min-height:34px}
  .prio{font-size:.72rem;font-weight:700;padding:2px 8px;border-radius:999px;align-self:flex-start}
  .prio.High{background:rgba(255,107,107,.16);color:#ff8a8a}
  .prio.Medium{background:rgba(244,183,64,.16);color:#f4b740}
  .prio.Low{background:rgba(62,207,142,.16);color:#3ecf8e}
  button{font:inherit;cursor:pointer;border:0;border-radius:10px;padding:10px 14px;font-weight:600}
  .btn{background:var(--accent);color:#fff}
  .btn:hover{filter:brightness(1.08)}
  .btn.ghost{background:transparent;border:1px solid var(--line);color:var(--text)}
  button:disabled{opacity:.5;cursor:default}
  details.custom{margin-top:14px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 16px}
  textarea{width:100%;min-height:150px;background:var(--panel2);color:var(--text);border:1px solid var(--line);
        border-radius:10px;padding:10px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.82rem}
  .progress{display:none;background:var(--panel);border:1px solid var(--line);border-radius:14px;
        padding:18px;margin-top:20px;box-shadow:var(--shadow);align-items:center;gap:14px}
  .progress.show{display:flex}
  .spinner{width:26px;height:26px;border:3px solid var(--line);border-top-color:var(--accent);
        border-radius:50%;animation:spin 1s linear infinite;flex:none}
  @keyframes spin{to{transform:rotate(360deg)}}
  .result{display:none;margin-top:20px}
  .result.show{display:block}
  .statbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}
  .badge{display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border-radius:999px;font-weight:700;font-size:.85rem}
  .badge.file{background:rgba(255,107,107,.16);color:#ff8a8a;border:1px solid rgba(255,107,107,.4)}
  .badge.review{background:rgba(244,183,64,.16);color:#f4b740;border:1px solid rgba(244,183,64,.4)}
  .badge.nofile{background:rgba(62,207,142,.16);color:#3ecf8e;border:1px solid rgba(62,207,142,.4)}
  .badge.ground{background:rgba(32,178,170,.16);color:#3fd0c7;border:1px solid rgba(32,178,170,.4)}
  .badge.ground.bad{background:rgba(255,107,107,.16);color:#ff8a8a;border-color:rgba(255,107,107,.4)}
  .chipmeta{color:var(--muted);font-size:.84rem}
  .conf{flex:1;min-width:140px}
  .confbar{height:8px;background:var(--line);border-radius:999px;overflow:hidden;margin-top:4px}
  .conffill{height:100%;background:linear-gradient(90deg,#7c5cff,#d97757)}
  .tabs{display:flex;gap:6px;border-bottom:1px solid var(--line);margin-bottom:14px;flex-wrap:wrap}
  .tab{background:none;color:var(--muted);border-radius:8px 8px 0 0;padding:9px 14px;font-weight:600}
  .tab.active{color:var(--text);border-bottom:2px solid var(--accent)}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:0 14px 14px 14px;
        padding:18px;box-shadow:var(--shadow)}
  .tabview{display:none} .tabview.active{display:block}
  .subject{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:8px}
  .subject b{font-size:.98rem}
  .chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
  .chip{background:var(--panel2);border:1px solid var(--line);border-radius:999px;padding:3px 9px;font-size:.76rem;color:var(--muted)}
  .chip.tx{font-family:ui-monospace,monospace;color:var(--aws)}
  .narr{white-space:pre-wrap;background:var(--panel2);border-radius:10px;padding:14px;font-size:.92rem}
  table{width:100%;border-collapse:collapse;font-size:.86rem}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--muted);font-weight:600}
  .flag{border-left:3px solid var(--file);padding:8px 12px;background:var(--panel2);border-radius:0 8px 8px 0;margin-bottom:8px}
  ol.trace{list-style:none;padding:0;margin:0}
  ol.trace li{display:flex;gap:12px;padding:8px 0;border-bottom:1px solid var(--line)}
  .tnum{width:26px;height:26px;border-radius:50%;background:var(--accent);color:#fff;display:flex;
        align-items:center;justify-content:center;font-size:.78rem;font-weight:700;flex:none}
  .tname{font-family:ui-monospace,monospace;font-weight:600}
  .tin{color:var(--muted);font-size:.8rem;font-family:ui-monospace,monospace}
  pre.raw{white-space:pre-wrap;word-break:break-word;background:var(--panel2);padding:14px;border-radius:10px;font-size:.78rem;max-height:520px;overflow:auto}
  .dl{display:flex;gap:8px;margin-top:14px}
  .ok{color:var(--nofile)} .err{color:var(--file)}
  ul.q{margin:6px 0 0;padding-left:18px} ul.q li{margin-bottom:4px}
  .foot{color:var(--muted);font-size:.8rem;margin-top:30px;text-align:center}
</style>
</head>
<body>
<header class="hero"><div class="wrap">
  <div>
    <h1>AML SAR Investigation Copilot</h1>
    <p>Claude investigates the case and drafts an evidence-cited Suspicious Activity Report.</p>
  </div>
  <div style="display:flex;align-items:center;gap:12px">
    <span class="pill">Human-in-the-loop · Claude on Amazon Bedrock</span>
    <a href="logout" style="color:#fff;opacity:.9;font-size:.85rem;font-weight:600;text-decoration:none;
       border:1px solid rgba(255,255,255,.35);padding:6px 12px;border-radius:999px">Sign out</a>
  </div>
</div></header>

<div class="wrap">
  <div class="disclaimer"><b>Decision-support draft, not a filing.</b> A qualified BSA/AML analyst must
     verify every fact and make the filing determination. Synthetic data only.</div>

  <h2 class="sec">1 · Choose a case</h2>
  <div id="cases" class="cases"><div class="chipmeta">Loading sample cases…</div></div>

  <details class="custom">
    <summary>Or paste your own case JSON</summary>
    <p class="chipmeta">Structure: {"case_id","subjects","accounts","transactions","alerts"}. Synthetic only.</p>
    <textarea id="caseInput" placeholder='{"case_id":"...","subjects":[...],"accounts":[...],"transactions":[...]}'></textarea>
    <div style="margin-top:10px"><button class="btn" id="customBtn">Draft SAR from pasted JSON</button></div>
  </details>

  <div id="progress" class="progress">
    <div class="spinner"></div>
    <div><div id="progressText">Investigating…</div>
    <div class="chipmeta" id="progressMeta"></div></div>
  </div>

  <div id="result" class="result">
    <h2 class="sec">2 · SAR draft</h2>
    <div class="statbar" id="statbar"></div>
    <div class="tabs" id="tabs">
      <button class="tab active" data-t="report">Report</button>
      <button class="tab" data-t="evidence">Evidence</button>
      <button class="tab" data-t="trace">Investigation</button>
      <button class="tab" data-t="raw">Raw JSON</button>
    </div>
    <div class="panel">
      <div class="tabview active" id="tv-report"></div>
      <div class="tabview" id="tv-evidence"></div>
      <div class="tabview" id="tv-trace"></div>
      <div class="tabview" id="tv-raw"></div>
      <div class="dl">
        <button class="btn ghost" id="dlMd">Download .md</button>
        <button class="btn ghost" id="dlJson">Download .json</button>
      </div>
    </div>
  </div>

  <div class="foot">Private architecture: CloudFront + OAC in front of an IAM-auth Lambda · async job API.</div>
</div>

<script>
let LAST = null, timer = null, t0 = 0;
const $ = id => document.getElementById(id);
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;');}
function recClass(r){return r==='recommend_file'?'file':(r==='needs_human_review'?'review':'nofile');}
function recLabel(r){return {recommend_file:'RECOMMEND FILE',needs_human_review:'NEEDS HUMAN REVIEW',recommend_no_file:'RECOMMEND NO FILE'}[r]||r;}
async function sha256hex(str){const b=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(str));
  return Array.from(new Uint8Array(b)).map(x=>x.toString(16).padStart(2,'0')).join('');}

async function loadCases(){
  try{
    const r = await fetch('cases',{cache:'no-store'}); const d = await r.json();
    const box = $('cases'); box.innerHTML='';
    (d.cases||[]).forEach(c=>{
      const el=document.createElement('div'); el.className='card';
      el.innerHTML=`<span class="prio ${c.priority}">${c.priority}</span>
        <h3>${esc(c.title)}</h3>
        <div class="meta">${esc(c.case_id)} · ${c.subject_count} subject(s) · ${c.txn_count} txns · ${c.alert_count} alert(s)</div>
        <div class="blurb">${esc(c.blurb)}</div>
        <button class="btn" data-key="${c.key}">Draft SAR</button>`;
      el.querySelector('button').onclick=()=>draft({case_key:c.key});
      box.appendChild(el);
    });
  }catch(e){ $('cases').innerHTML='<div class="err">Could not load cases: '+esc(''+e)+'</div>'; }
}

function startTimer(){ t0=Date.now(); timer=setInterval(()=>{ $('progressMeta').textContent=((Date.now()-t0)/1000).toFixed(0)+'s elapsed'; },500); }
function stopTimer(){ clearInterval(timer); }

async function draft(body){
  $('result').classList.remove('show');
  $('progress').classList.add('show'); $('progressText').textContent='Submitting job…';
  document.querySelectorAll('button').forEach(b=>b.disabled=true);
  startTimer();
  try{
    const s=JSON.stringify(body); const h=await sha256hex(s);
    const r=await fetch('draft',{method:'POST',headers:{'content-type':'application/json','x-amz-content-sha256':h},body:s});
    const d=await r.json();
    if(d.error){ fail(d.error); return; }
    $('progressText').textContent='Claude is investigating the case…';
    poll(d.job_id,60);
  }catch(e){ fail(''+e); }
}
function fail(msg){ stopTimer(); $('progressText').innerHTML='<span class="err">Error: '+esc(msg)+'</span>';
  document.querySelectorAll('button').forEach(b=>b.disabled=false); }

async function poll(job,tries){
  if(tries<=0){ fail('Timed out.'); return; }
  try{
    const r=await fetch('result?job_id='+encodeURIComponent(job),{cache:'no-store'});
    const d=await r.json();
    if(d.status==='pending'){ setTimeout(()=>poll(job,tries-1),3000); return; }
    stopTimer(); document.querySelectorAll('button').forEach(b=>b.disabled=false);
    if(d.status==='error'){ fail(d.error||'worker error'); return; }
    LAST=d; render(d);
  }catch(e){ setTimeout(()=>poll(job,tries-1),3000); }
}

function render(d){
  $('progress').classList.remove('show');
  const g=d.grounding||{}; const halluc=(g.hallucinated||[]).length;
  const conf=d.confidence!=null?Math.round(d.confidence*100):null;
  $('statbar').innerHTML =
    `<span class="badge ${recClass(d.filing_recommendation)}">${recLabel(d.filing_recommendation)}</span>
     <span class="badge ground ${halluc?'bad':''}">${halluc?'⚠':'✓'} ${g.verified||0}/${g.cited||0} txns verified · ${halluc} fabricated</span>
     <div class="conf"><div class="chipmeta">Confidence ${conf!=null?conf+'%':'n/a'}</div>
        <div class="confbar"><div class="conffill" style="width:${conf||0}%"></div></div></div>
     <span class="chipmeta">${esc(d.case_id)} · ${d.rounds} rounds · ${d.elapsed_seconds}s · ${esc((d.model_id||'').split('.').pop())}</span>`;

  const s=d.sar||{};
  // Report
  $('tv-report').innerHTML =
    '<h2 class="sec">Subjects</h2>'+(s.subjects||[]).map(x=>
      `<div class="subject"><b>${esc(x.name)}</b> — ${esc(x.role||'')}
       <div class="chips">${(x.identifiers||[]).map(i=>'<span class="chip">'+esc(i)+'</span>').join('')}</div></div>`).join('')+
    '<h2 class="sec">Typologies</h2><div class="chips">'+
      (s.suspicious_typologies||[]).map(t=>'<span class="chip">'+esc(t)+'</span>').join('')+
      (!(s.suspicious_typologies||[]).length?'<span class="chipmeta">none identified</span>':'')+'</div>'+
    '<h2 class="sec">Activity summary</h2><div class="narr">'+esc(s.activity_summary)+'</div>'+
    '<h2 class="sec">Narrative</h2><div class="narr">'+esc(s.narrative)+'</div>'+
    ((s.unresolved_questions||[]).length?'<h2 class="sec">Open questions for the analyst</h2><ul class="q">'+
      s.unresolved_questions.map(q=>'<li>'+esc(q)+'</li>').join('')+'</ul>':'');

  // Evidence
  $('tv-evidence').innerHTML =
    '<h2 class="sec">Red flags</h2>'+((s.red_flags||[]).map(f=>
      `<div class="flag">${esc(f.flag)}<div class="chips">${(f.supporting_txn_ids||[]).map(i=>'<span class="chip tx">'+esc(i)+'</span>').join('')}</div></div>`).join('')||'<div class="chipmeta">none</div>')+
    '<h2 class="sec">Evidence map</h2><table><tr><th>Claim</th><th>Transactions</th></tr>'+
      (s.evidence||[]).map(e=>`<tr><td>${esc(e.claim)}</td><td>${(e.txn_ids||[]).map(i=>'<span class="chip tx">'+esc(i)+'</span>').join(' ')}</td></tr>`).join('')+
      (!(s.evidence||[]).length?'<tr><td colspan="2" class="chipmeta">no citations (nothing to file)</td></tr>':'')+'</table>';

  // Trace
  $('tv-trace').innerHTML='<h2 class="sec">How Claude investigated</h2><ol class="trace">'+
    (d.trace||[]).map((t,i)=>`<li><span class="tnum">${i+1}</span><div><span class="tname">${esc(t.tool)}</span>
      ${Object.keys(t.input||{}).length?'<div class="tin">'+esc(JSON.stringify(t.input))+'</div>':''}</div></li>`).join('')+'</ol>';

  // Raw
  $('tv-raw').innerHTML='<pre class="raw">'+esc(JSON.stringify(s,null,2))+'</pre>';
  $('result').classList.add('show');
  $('result').scrollIntoView({behavior:'smooth',block:'start'});
}

function download(name,text,type){const b=new Blob([text],{type});const u=URL.createObjectURL(b);
  const a=document.createElement('a');a.href=u;a.download=name;a.click();URL.revokeObjectURL(u);}

// tabs
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.tabview').forEach(x=>x.classList.remove('active'));
  t.classList.add('active'); $('tv-'+t.dataset.t).classList.add('active');
});
$('customBtn').onclick=()=>{const txt=$('caseInput').value.trim();
  if(!txt){alert('Paste case JSON or pick a sample.');return;}
  let p; try{p=JSON.parse(txt);}catch(e){alert('Invalid JSON: '+e);return;} draft({case:p});};
$('dlMd').onclick=()=>LAST&&download((LAST.case_id||'sar')+'.md',LAST.sar_markdown||'','text/markdown');
$('dlJson').onclick=()=>LAST&&download((LAST.case_id||'sar')+'.json',JSON.stringify(LAST.sar||{},null,2),'application/json');
loadCases();
</script>
</body>
</html>"""



LOGIN_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Sign in · AML SAR Copilot</title>
<style>
  :root{--accent:#7c5cff;--clay:#d97757;--text:#e7e9ee;--muted:#9aa3b2;--panel:#171a23;--line:#2a2f3c;--err:#ff6b6b}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
    font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--text);
    background:radial-gradient(1200px 600px at 20% -10%, #2a1e5c 0%, #0f1117 55%);
    background-color:#0f1117}
  .card{width:360px;max-width:92vw;background:rgba(23,26,35,.9);border:1px solid var(--line);
    border-radius:18px;padding:30px 28px;box-shadow:0 24px 60px rgba(0,0,0,.5);backdrop-filter:blur(6px)}
  .brand{display:flex;align-items:center;gap:10px;margin-bottom:6px}
  .logo{width:38px;height:38px;border-radius:10px;background:linear-gradient(135deg,#7c5cff,#d97757)}
  h1{font-size:1.15rem;margin:0}
  p.sub{color:var(--muted);font-size:.86rem;margin:4px 0 22px}
  label{display:block;font-size:.8rem;color:var(--muted);margin:12px 0 6px}
  input{width:100%;background:#0f1117;border:1px solid var(--line);border-radius:10px;color:var(--text);
    padding:11px 12px;font-size:.95rem}
  input:focus{outline:none;border-color:var(--accent)}
  button{width:100%;margin-top:20px;background:var(--accent);color:#fff;border:0;border-radius:10px;
    padding:12px;font-size:.98rem;font-weight:700;cursor:pointer}
  button:disabled{opacity:.6;cursor:default}
  .err{color:var(--err);font-size:.85rem;min-height:20px;margin-top:12px}
  .foot{color:var(--muted);font-size:.72rem;margin-top:18px;text-align:center}
</style>
</head>
<body>
  <form class="card" id="f">
    <div class="brand"><div class="logo"></div><h1>AML SAR Copilot</h1></div>
    <p class="sub">Sign in to draft Suspicious Activity Report narratives.</p>
    <label for="u">Username</label>
    <input id="u" autocomplete="username" autofocus/>
    <label for="p">Password</label>
    <input id="p" type="password" autocomplete="current-password"/>
    <button id="btn" type="submit">Sign in</button>
    <div class="err" id="err"></div>
    <div class="foot">Authorized use only · Synthetic data · Human-in-the-loop</div>
  </form>
<script>
async function sha256hex(str){const b=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(str));
  return Array.from(new Uint8Array(b)).map(x=>x.toString(16).padStart(2,'0')).join('');}
document.getElementById('f').addEventListener('submit', async (e)=>{
  e.preventDefault();
  const btn=document.getElementById('btn'), err=document.getElementById('err');
  err.textContent=''; btn.disabled=true; btn.textContent='Signing in…';
  const body=JSON.stringify({username:document.getElementById('u').value,password:document.getElementById('p').value});
  try{
    const h=await sha256hex(body);
    const r=await fetch('login',{method:'POST',headers:{'content-type':'application/json','x-amz-content-sha256':h},body});
    if(r.ok){ location.href='/'; return; }
    const d=await r.json().catch(()=>({}));
    err.textContent=d.error||('Sign in failed ('+r.status+')');
  }catch(ex){ err.textContent='Request failed: '+ex; }
  btn.disabled=false; btn.textContent='Sign in';
});
</script>
</body>
</html>"""
