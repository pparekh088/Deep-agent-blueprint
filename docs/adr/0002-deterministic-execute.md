# ADR-0002: /execute is deterministic — no LLM after approval

**Status:** Accepted · **Applies to:** template core + every adapter

## Context

Every mutation requires explicit human approval in the consumer's UI. If any
LLM call sits between the human's "approve" and the mutation, what executes
is no longer what was approved — the model could rephrase a comment, pick a
different field value, or hallucinate a target. In a regulated environment
that gap is an audit finding, not a bug.

## Decision

`POST /execute` contains **zero LLM calls**. It: validates the approved
payload against the stored proposal (byte-for-byte outside explicitly
editable fields) and the adapter's Pydantic schema, re-checks live target
preconditions, then performs one direct API call via the adapter. The
research phase must therefore emit **fully self-contained** payloads —
everything execution needs is in the proposal.

## Consequences

- What the human saw and approved is exactly what executes; `409
  PAYLOAD_MISMATCH` / `409 STALE_TARGET` guard the two drift windows
  (payload tampering, target drift).
- Proposals must be complete at research time; "the executor will figure it
  out" is not available. This pushes quality pressure onto the research
  prompt's output contract — deliberately.
- Mutations are fast and synchronous, so no second job model is needed.
