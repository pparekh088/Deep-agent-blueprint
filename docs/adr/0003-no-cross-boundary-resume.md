# ADR-0003: No LangGraph interrupt/resume across the service boundary

**Status:** Accepted · **Applies to:** template core

## Context

LangGraph supports human-in-the-loop via interrupts: pause the graph at the
mutation, checkpoint state, resume after approval. It is tempting to use it
here — the harness "supports it natively."

## Decision

The agent run **ends when research ends**. We never hold a checkpointed
graph waiting for the consumer's approval. The proposed action returned by
research is self-contained; `/execute` is a fresh, stateless, deterministic
call (ADR-0002).

## Rationale (the failures this prevents)

- **Held state = held liability.** A paused graph pins worker memory or a
  checkpoint store for however long a human takes to decide (minutes to
  never). That inflates infra, complicates autoscaling (KEDA can't scale
  down pods holding checkpoints), and creates an orphan-cleanup problem.
- **Coupled contract.** Resume tokens leak the harness into the API — the
  consumer would hold a LangGraph-shaped handle, breaking harness
  swappability (ADR-0006) forever.
- **Version fragility.** A checkpoint written by `langgraph==X` may not
  resume under `X+1`; deploys would strand every pending approval.
- **Security.** Resuming as the user would require holding the PAT (or a
  resumable credential) for the whole approval window — worse than the
  bounded staging exception in ADR-0005.

## Consequences

- Approval latency is unbounded without cost to us; proposals simply expire
  (TTL) and research can be re-run.
- If target state changes during approval, the precondition re-check catches
  it (`409 STALE_TARGET`) instead of a stale checkpoint silently acting.
