# ADR-0005: The Key Vault token-staging exception

**Status:** Accepted · **Applies to:** USER_PAT domains only

## Context

The rule is: credentials are request-scoped, never persisted. The async job
model (ADR-0001) breaks this for USER_PAT domains — the worker picks the job
up seconds later, on another pod, and must act as the submitting user. The
PAT must cross a process boundary somehow.

Options considered: (a) consumer re-sends the PAT on a worker callback —
inverts the connection model, still persists a pending-callback secret;
(b) plaintext PAT in the queue payload — one Redis dump away from mass
credential disclosure; (c) envelope-encrypted staging — chosen.

## Decision

A **single, bounded, documented exception**: on submit, the PAT is envelope-
encrypted (fresh per-job AES-256-GCM DEK, wrapped by an Azure Key Vault key
via `CryptographyClient`; the KEK never leaves Key Vault) and stored at
`{domain}:tok:{job_id}` with TTL = max job lifetime. The worker unwraps at
run start, holds plaintext in memory only, and injects it into read-tool
headers. The ciphertext is **purged on any terminal state** — TTL is only
the crash backstop.

Boundaries that keep the exception narrow:

- `/execute` always uses the PAT from the live request header — never a
  staged token.
- Poll/cancel authorize via a **salted hash** of the presented token, never
  by comparing tokens.
- Key Vault unreachable ⇒ **fail closed**: submission rejected (503), never
  weaker encryption or plaintext fallback.
- The plaintext never appears in Redis, logs, traces, results, or proposals
  (redaction registry + contract tests enforce this).

## Consequences

- Compromising Redis alone yields nothing usable; an attacker needs Redis
  **and** Key Vault unwrap rights within the job's lifetime.
- Key Vault becomes an availability dependency of `POST /research` for
  USER_PAT domains (accepted; see failure-mode table in BLUEPRINT.md).
- Every future "can we just cache the token?" conversation ends here: no —
  this ADR is the only sanctioned persistence, and only in this shape.
