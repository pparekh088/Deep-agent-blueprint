# ADR-0004: Per-domain auth_mode instead of assuming a user PAT

**Status:** Accepted · **Applies to:** template core + every adapter

## Context

Jira/Confluence/Email act on the target system **as the end user** (the
consumer forwards a personal access token). Web search uses a service-owned
API key. Future domains may need no downstream credential at all. The first
template draft assumed a PAT everywhere; the web search domain immediately
broke that assumption — and a PAT-shaped hole in a no-PAT domain is a
security bug (tokens accepted, staged, and never needed).

## Decision

Every `DomainAdapter` declares exactly one `auth_mode`:

| mode | credential | source | staged in Redis? |
|---|---|---|---|
| `USER_PAT` | user's PAT | `X-User-Token` header per request | ciphertext only, job-scoped (ADR-0005) |
| `SERVICE_CREDENTIAL` | service-owned key | env/Key Vault config | never |
| `NONE` | none | — | never |

All request validation, token staging, poll/execute authorization, and
credential resolution branch on `auth_mode` in template core
(`app/auth/user_token.py`, `app/adapters/__init__.py:resolve_credentials`).
Adapters never read headers; endpoints never touch credentials directly.

## Consequences

- A `SERVICE_CREDENTIAL` domain ignores `X-User-Token` entirely: it is never
  validated, staged, or logged (contract-tested).
- Poll/cancel authorization differs by mode: salted principal hash
  (`USER_PAT`) vs. consumer-ID binding (others) — both in core, chosen by
  the declared mode.
- Adding a new mode (e.g. OAuth on-behalf-of) is a core change with an ADR,
  not an adapter hack.
