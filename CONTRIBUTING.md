# Contributing

Two kinds of change land here: **shipping a new domain agent** (the common
case — you copy this template) and **changing template core** (rare —
coordinated, versioned, back-ported).

## Shipping a new domain agent — Definition of Done

Run `make new-domain NAME=<domain>` to scaffold, then work this checklist.
**Every box is required.** A domain agent that skips one is not done.

### Adapter
- [ ] `app/adapters/<domain>.py` implements the `DomainAdapter` ABC
      completely — no `NotImplementedError` left outside `service_credentials`
      (which only `SERVICE_CREDENTIAL` domains override).
- [ ] `auth_mode` is declared deliberately (`USER_PAT` / `SERVICE_CREDENTIAL`
      / `NONE`) and matches how the target system is actually accessed.
      If `SERVICE_CREDENTIAL`: the credential comes from config only.
- [ ] Every mutation has a Pydantic payload schema in `action_schemas()`;
      `editable_fields()` returns only fields a human may safely reword.
- [ ] `check_preconditions` re-reads live target state and detects drift
      (version / status / timestamp) for every action type.
- [ ] Read tools use `read_request_with_backoff` for 429/5xx; executors
      never auto-retry.

### Read-only verification
- [ ] Confirm by inspection that `read_tools()` returns **zero** callables
      that mutate — no create/update/delete/send reachable from research.
      The contract suite checks tool identity; you check semantics.

### Tests
- [ ] A `ContractCase` for the domain is registered in
      `tests/contract/cases.py` (offline `httpx.MockTransport`, canned agent
      output; `control` dict if the domain has mutations, so stale-target is
      exercised).
- [ ] `make test-contract` passes **unchanged** — if you had to edit anything
      under `tests/contract/` other than `cases.py`, stop and file template
      feedback instead.
- [ ] Adapter-specific unit tests in `tests/unit/test_<domain>_adapter.py`
      cover: schema validation, editable fields, precondition drift, each
      executor, downstream-error surfacing.

### Security & observability
- [ ] `log_content_allowlist` reviewed: only identifiers, never user content.
- [ ] For `USER_PAT`: verified in a dev environment that the PAT appears
      nowhere in Redis (`redis-cli --scan | xargs -n1 redis-cli get`), logs,
      or results, and `{domain}:tok:*` is purged on completion/cancel/failure.
- [ ] Splunk log schema verified in dev: events arrive as single-line JSON
      with the canonical keys; `correlation_id` joins submit→execute.

### Config & docs
- [ ] Domain config fields appended to the DOMAIN-SPECIFIC block in
      `app/config.py` and `.env.example`.
- [ ] ADR added under `docs/adr/` **if** the domain deviated from the
      template anywhere (none needed for a clean instantiation).
- [ ] Runbook entry: dashboard links, common downstream failures, rate-limit
      characteristics, on-call notes.
- [ ] Repo records the `TEMPLATE_VERSION` it was cut from (README table).

## Changing template core

Everything outside `app/adapters/` and `app/agent/prompts.py` is
**TEMPLATE_CORE**. If shipping your domain requires editing core, the
template has failed — file template feedback (issue against this repo), do
not fork. Core changes must:

1. Keep the contract suite green for **all** registered cases.
2. Bump `TEMPLATE_VERSION` in `app/__init__.py` and note the change in the
   README changelog table.
3. Add/update an ADR if the change alters a recorded decision.
4. Be announced so domain repos can back-port deliberately.

## Conventions

- Python 3.12, fully type-hinted, async throughout.
- Exact version pins (regulated environment); the agent-harness pin set
  (`deepagents`/`langchain`/`langgraph`) moves only as a set (ADR-0006).
- Log via `log_event(...)` with canonical fields; never log payload content
  outside the reviewed allowlist.
