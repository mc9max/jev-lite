# Deploy and Host

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.com/deploy/jev-lite-1)

**Jev Lite** is a self-hosted proxy + web console for [TypeSafe's Jev (System One)](https://console.typesafe.ai) decision API — turn freeform application state and a set of structured questions into a typed model call, then read back a clean JSON verdict.

This template ships the whole client in **one container**: a thin FastAPI proxy plus a single-file web console. No model weights, no GPU, no database, no companion service — the container is stateless and lightweight (~180 MB). Your TypeSafe API key stays server-side; the browser never sees it.

- Web console: **/** (build state + questions, read the verdict)
- API: **`POST /v1/decide`** with `{state, questions, model?}` (also **`GET /v1/status`**, **`GET /v1/models`**)
- **`/health`** — liveness + `key_configured` flag, unauthenticated (Railway healthcheck target)
- Optional lock-down: **`AUTH_TOKEN`**, when set, gates the UI and every `/v1` endpoint with `Authorization: Bearer ***` (or `?token=***` on the UI URL)

## Why Deploy

Jev (System One) is TypeSafe's hosted decision model — you hand it the current state of an application plus a small set of typed questions (yes/no, choice, score) and get back a structured verdict with per-question answers. That is the shape of a lot of "should I take this action?" logic, but it needs a stable, typed surface between your front-end and the hosted model.

- **One click** — no model to host, no GPU, no database, no companion service. One stateless container on the Hobby plan
- **Server-side key** — the proxy holds `TYPESAFE_API_KEY` and forwards it to the model; the browser and the deploy page never see it
- **Typed, verifiable API** — requests are validated locally (fast, clear 400s with a shape hint), forwarded as-is, and upstream error semantics are mapped to stable client errors
- **Web console** — a single self-contained page that builds the state + questions and shows the verdict, with optional token lock-down
- **No state to lose** — no volume, no storage; a redeploy is a clean re-run

## About Hosting

Single service, Dockerfile build from the pinned `python:3.12-slim` base:

1. **Pinned dependency layer** — `fastapi`, `uvicorn[standard]`, `httpx` installed once; only the app code changes
2. **Non-root runtime** — the container runs as uid 1000. There is no volume mounted, so there is nothing root-owned to collide with (the Railway volume EACCES trap only applies to mounted paths)
3. **Shell-form CMD** — `exec uvicorn … --port ${PORT:-8080}` so the `PORT` variable Railway injects is expanded at container start
4. **In-image healthcheck** — `python` probes `/health` on `PORT` with a 20 s start period, matching the `railway.json` builder

No volume is created: the client is fully stateless.

## Dependencies for Jev Lite

### Deployment Dependencies

One external service — **TypeSafe Jev (System One)**. All the app needs from it is an API key and outbound internet access to `api.typesafe.ai`:

| Variable | Required | Where to get it |
|---|---|---|
| `TYPESAFE_API_KEY` | **Yes** (for `/v1/decide`) | https://console.typesafe.ai → **Settings → Keys** |
| `JEV_MODEL` | No (default `jev-latest`) | A Jev model id; `jev-latest` tracks the newest release |
| `AUTH_TOKEN` | No (blank = open) | Any secret you choose, if you want to lock the instance down |

The service **boots and serves the UI and `/health` without a key** — `/v1/decide` and `/v1/models` return a clear `no_api_key` error (with a hint pointing at the console) until you set `TYPESAFE_API_KEY` and redeploy. So you can always confirm the deployment is live before wiring in a key.

## Ports

- `8080` — web console + API; `/health` for the Railway healthcheck

## Common Use Cases

- A private decision endpoint in front of TypeSafe, with your front-end posting typed questions instead of raw prompt text
- An opinionated, validated proxy that keeps your client key server-side and returns stable, documented error codes
- A lockable web console (via `AUTH_TOKEN`) for a shared instance where only token holders can drive `/v1/decide`
- A reference shape for wrapping a hosted "state + questions → verdict" model behind a small, auditable API
