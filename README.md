# Domain Deep Agent Service — golden-path template

**TEMPLATE_VERSION: 1.0.0** (also in `app/__init__.py`; record the version
your domain repo was cut from in the table below)

One deployable per domain (Jira, Confluence, Email, web search, …) that wraps
a LangChain **Deep Agents** research agent behind a clean HTTP API:

- **`POST /research`** — async job (202 + `job_id`), agentic, **strictly
  read-only** research in a background worker; poll `GET /research/{job_id}`.
- **`POST /execute`** — synchronous, **deterministic** (no LLM), runs one
  human-approved mutation with precondition re-checks and idempotency keys.

The consumer (an external LangGraph chat orchestrator) owns all approval UX.
Read [`BLUEPRINT.md`](BLUEPRINT.md) before touching anything — it is the
engineering standard this template implements.

## Quickstart (local)

```bash
# 1. deps (Python 3.12)
make install

# 2. Redis
make redis                        # docker, foreground; or your own redis:6379

# 3. config
cp .env.example .env              # then edit:
#    DOMAIN=jira|websearch, API_KEYS, AZURE_OPENAI_*,
#    USER_PAT domains: LOCAL_CRYPTO_KEY_B64=$(python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())")

# 4. run both tiers (two shells)
make run-api                      # uvicorn on :8000
make run-worker                   # arq worker

# 5. exercise it
curl -s -X POST localhost:8000/research \
  -H 'X-Api-Key: dev-only-key-change-me' \
  -H 'X-User-Token: <your-jira-pat>' \
  -H 'Content-Type: application/json' \
  -d '{"task": "Summarize open bugs in PROJ", "session_id": "s1"}'
# → {"job_id": "...", "poll_url": "/research/...", ...}
curl -s localhost:8000/research/<job_id> -H 'X-Api-Key: ...' -H 'X-User-Token: ...'
```

Tests need no Redis, Azure, or LLM: `make test` (or `make test-contract`).

## Core vs. customize — the boundary

Everything not listed in the first table is **TEMPLATE_CORE** — copy it
unmodified. If your domain needs a core edit, the template has failed: file
template feedback, don't fork (see CONTRIBUTING.md).

| You touch (per new domain) | Purpose |
|---|---|
| `app/adapters/<domain>.py` | tools, schemas, executors, preconditions (scaffolded) |
| `app/adapters/__init__.py` | one builder entry (scaffold inserts it) |
| `app/agent/prompts.py` | only if `research_instructions()` isn't enough |
| `app/config.py` + `.env.example` | append DOMAIN-SPECIFIC fields |
| `tests/contract/cases.py` | your domain's ContractCase (scaffold stubs it) |
| `tests/unit/test_<domain>_adapter.py` | adapter unit tests (scaffolded) |

| You never touch | |
|---|---|
| `app/api/`, `app/worker/`, `app/main.py` | HTTP contract, job loop |
| `app/auth/`, `app/state/`, `app/observability/` | auth modes, Redis, token vault, logging |
| `app/agent/factory.py`, `app/llm/` | harness + LLM construction |
| `app/models/schemas.py` | the API contract itself |
| `tests/contract/*` (except `cases.py`) | the shared gate — must pass unchanged |

## Adding a new domain (5 steps)

```bash
make new-domain NAME=confluence
```

1. **Implement the adapter** (`app/adapters/confluence.py`): declare
   `auth_mode`, write read-only tools.
2. **Register schemas**: one Pydantic model per mutation in
   `action_schemas()`; pick `editable_fields` deliberately.
3. **Define executors + preconditions**: `execute()` and
   `check_preconditions()` against the live system.
4. **Make the contract suite green**: flesh out your `ContractCase`
   (offline mocks) and run `make test` — `tests/contract/` must pass
   **unchanged**.
5. **Deploy**: set `DOMAIN=confluence` (+ your config) on the API and worker
   deployments — same image, two entrypoints (see `Dockerfile`).

Then complete the Definition of Done in [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Repository map

```
app/            service (core + adapters — see boundary table above)
tests/contract/ domain-agnostic gate: API shape, auth modes, lifecycle,
                idempotency, token security, correlation
tests/unit/     core + per-adapter tests
docs/adr/       the decisions you must not accidentally undo
scripts/        new_domain.py scaffold
BLUEPRINT.md    the architecture standard (start here)
```

## Domain repos cut from this template

| Domain | Repo | TEMPLATE_VERSION |
|---|---|---|
| jira (reference) | this repo | 1.0.0 |
| websearch (reference) | this repo | 1.0.0 |
