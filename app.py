"""Jev Lite — self-hosted proxy + web console for TypeSafe's Jev (System One) API.

Design rules (why this shape):
  * Server-side key. The browser never sees TYPESAFE_API_KEY. The UI posts to
    our /v1/decide endpoint and we forward to the upstream with the key.
  * Thin, typed, verifiable proxy. We validate the request shape locally
    (fast, clear 400s), forward as-is, and map upstream error semantics to
    stable client errors. No header pass-through, no upstream identity leakage
    beyond a friendly hint string.
  * Stateless. No volume, no database. One container on the Hobby plan.
  * Optional lock-down: AUTH_TOKEN, when set, gates the UI and API with
    'Authorization: Bearer <token>' (or ?token=*** for UI navigation).

Endpoints:
  GET  /            -> web console (static/index.html)
  GET  /health      -> liveness + key status  (NO auth — Railway healthcheck)
  GET  /v1/status   -> key/model/upstream status (auth-gated)
  GET  /v1/models   -> upstream model list pass-through (auth-gated)
  POST /v1/decide   -> forward state+questions to the upstream, add latency_ms
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

UPSTREAM = os.environ.get("JEV_API_BASE", "https://api.typesafe.ai").rstrip("/")
API_KEY = os.environ.get("TYPESAFE_API_KEY", "").strip()
DEFAULT_MODEL = os.environ.get("JEV_MODEL", "jev-latest").strip() or "jev-latest"
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "").strip()
TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "60"))

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Disable FastAPI's auto docs surface — this is a product, not an API shell.
app = FastAPI(title="Jev Lite", version="1.0.0",
              docs_url=None, redoc_url=None, openapi_url=None)

VALID_QUESTION_TYPES = ("noul", "choice", "score")


# ── helpers ──────────────────────────────────────────────────────────────────

def _client_auth_ok(request: Request) -> bool:
    if not AUTH_TOKEN:
        return True
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() == AUTH_TOKEN
    return False


def _auth_response() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "error": "unauthorized",
            "hint": "This instance is locked down. Send an "
                    "'Authorization: Bearer <AUTH_TOKEN>' header "
                    "(or open the UI with ?token=***",
        },
    )


def _no_key_response() -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={
            "error": "no_api_key",
            "hint": "This instance was deployed without TYPESAFE_API_KEY. "
                    "Create a key at console.typesafe.ai (Settings -> Keys), "
                    "set it on this service's variables, and redeploy.",
        },
    )


def _upstream_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if API_KEY:
        h["Authorization"] = f"Bearer {API_KEY}"
    return h


def _bad_shape(message: str, hint: str = "") -> JSONResponse:
    body: dict = {"error": "invalid_request", "detail": message}
    if hint:
        body["hint"] = hint
    return JSONResponse(status_code=400, content=body)


def _map_upstream_error(status: int, detail) -> tuple[int, dict]:
    """Stable client-side error mapping for upstream responses."""
    if isinstance(detail, dict):
        msg = str(detail.get("message") or detail.get("detail") or "")
    else:
        msg = str(detail or "")[:300]

    if status in (401, 403):
        content: dict = {
            "error": "upstream_key_rejected",
            "hint": "TypeSafe rejected the configured TYPESAFE_API_KEY. "
                    "Verify the key at console.typesafe.ai and redeploy.",
        }
        if not API_KEY:
            content = {
                "error": "no_api_key",
                "hint": "TYPESAFE_API_KEY is empty on this service. Add a key "
                        "from console.typesafe.ai (Settings -> Keys) and "
                        "redeploy.",
            }
        return 502, content
    if status == 422:
        return 400, {
            "error": "invalid_request",
            "detail": msg or "upstream rejected the request shape",
            "hint": "Request body: {state, questions: {<id>: {type: "
                    "noul|choice|score, instructions, criteria?}}, model?}. "
                    "See the API section on the home page.",
        }
    if status == 429:
        return 429, {
            "error": "rate_limited",
            "hint": "Upstream rate limit hit (1,200 requests/min, "
                    "250k tokens/s). Back off and retry.",
        }
    if status == 529:
        return 503, {
            "error": "upstream_overloaded",
            "hint": "The model service is temporarily overloaded. Retry in a "
                    "few seconds.",
        }
    if status >= 500:
        return 502, {
            "error": "upstream_error",
            "detail": msg or f"upstream returned HTTP {status}",
        }
    return status, {"error": "upstream_error", "detail": msg}


# ── routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    """Liveness. Intentionally unauthenticated (Railway healthcheck target).
    Never leaks the key — only a boolean."""
    return {
        "status": "ok",
        "key_configured": bool(API_KEY),
        "model": DEFAULT_MODEL,
        "upstream": UPSTREAM,
    }


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(html)


@app.get("/v1/status")
def status(request: Request):
    if not _client_auth_ok(request):
        return _auth_response()
    return {
        "status": "ok",
        "key_configured": bool(API_KEY),
        "model": DEFAULT_MODEL,
        "upstream": UPSTREAM,
    }


@app.get("/v1/models")
async def models(request: Request):
    if not _client_auth_ok(request):
        return _auth_response()
    if not API_KEY:
        return _no_key_response()
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{UPSTREAM}/v1/models",
                                 headers=_upstream_headers())
    except httpx.HTTPError as exc:
        return JSONResponse(502, {
            "error": "upstream_unreachable",
            "detail": str(exc)[:300],
        })
    latency_ms = round((time.perf_counter() - t0) * 1000)
    if r.status_code >= 400:
        status_code, content = _map_upstream_error(r.status_code, _safe_json(r))
        content["latency_ms"] = latency_ms
        return JSONResponse(status_code=status_code, content=content)
    data = _safe_json(r)
    if not isinstance(data, dict):
        data = {"models": data}
    data["latency_ms"] = latency_ms
    return JSONResponse(status_code=200, content=data)


@app.post("/v1/decide")
async def decide(request: Request):
    if not _client_auth_ok(request):
        return _auth_response()
    if not API_KEY:
        return _no_key_response()

    try:
        body = await request.json()
    except Exception:
        return _bad_shape(
            "body must be a JSON object",
            "Shape: {\"state\": string|object, \"questions\": {<id>: "
            "{type, instructions, criteria?}}, \"model\"?: string}")
    if not isinstance(body, dict):
        return _bad_shape("body must be a JSON object")

    state = body.get("state")
    if state is None or state == "":
        return _bad_shape("'state' is required",
                          "Send the application context to evaluate: a "
                          "string, JSON object, or array of text.")
    if not isinstance(state, (str, dict, list)):
        return _bad_shape("'state' must be string, object, or array")

    questions = body.get("questions")
    if not isinstance(questions, dict) or not questions:
        return _bad_shape(
            "'questions' is required: a non-empty map of "
            "<question-id> -> typed question object")
    for qid, q in questions.items():
        if not isinstance(q, dict):
            return _bad_shape(f"question '{qid}' must be an object")
        qtype = q.get("type")
        if qtype not in VALID_QUESTION_TYPES:
            return _bad_shape(
                f"question '{qid}' has invalid type {qtype!r}",
                "Use \"noul\" (yes/no), \"choice\" (pick an option), or "
                "\"score\" (rate on a rubric).")
        if not isinstance(q.get("instructions"), str) or not q["instructions"].strip():
            return _bad_shape(
                f"question '{qid}' needs a non-empty 'instructions' string")
        if qtype == "choice":
            crit = q.get("criteria")
            if not isinstance(crit, dict) or len(crit) < 2:
                return _bad_shape(
                    f"question '{qid}' (choice) needs 'criteria' as a map of "
                    "at least 2 options")
        if qtype == "score":
            crit = q.get("criteria")
            if not isinstance(crit, list) or len(crit) < 2:
                return _bad_shape(
                    f"question '{qid}' (score) needs 'criteria' as a list of "
                    "at least 2 ordered levels")

    model = body.get("model") or DEFAULT_MODEL
    if not isinstance(model, str) or not model.strip():
        return _bad_shape("'model' must be a non-empty string when provided")
    payload: dict = {
        "state": state,
        "questions": questions,
        "model": model,
    }
    # Optional per-question instructions live inside each question object and
    # are already forwarded verbatim.

    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(f"{UPSTREAM}/v1/systemone",
                                  json=payload, headers=_upstream_headers())
    except httpx.HTTPError as exc:
        return JSONResponse(502, {
            "error": "upstream_unreachable",
            "detail": str(exc)[:300],
            "hint": "Check network access to api.typesafe.ai and the "
                    "UPSTREAM_TIMEOUT setting.",
        })
    latency_ms = round((time.perf_counter() - t0) * 1000)

    if r.status_code >= 400:
        status_code, content = _map_upstream_error(r.status_code, _safe_json(r))
        content["latency_ms"] = latency_ms
        return JSONResponse(status_code=status_code, content=content,
                            headers={"X-Latency-Ms": str(latency_ms)})

    data = _safe_json(r)
    if not isinstance(data, dict):
        data = {"raw": str(data)}
    content = {**data, "latency_ms": latency_ms, "model_echo": model}
    return JSONResponse(status_code=200, content=content,
                        headers={"X-Latency-Ms": str(latency_ms)})


def _safe_json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except Exception:
        return {"message": r.text[:300]}


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    return JSONResponse(500, {
        "error": "internal_error",
        "detail": f"{type(exc).__name__}: {exc}"[:200],
    })
