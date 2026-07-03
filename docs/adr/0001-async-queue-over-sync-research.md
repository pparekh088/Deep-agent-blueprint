# ADR-0001: Research is an async job on a Redis queue, not a sync request

**Status:** Accepted · **Applies to:** template core

## Context

A deep agent research run takes minutes: multiple LLM calls, tool round
trips, planning. The service sits behind APIM/AKS ingress that will not hold
long-lived synchronous connections (gateway timeouts are typically 60–240s
and not ours to change). We also need backpressure when a burst of research
requests arrives, and worker crash recovery.

## Decision

`POST /research` returns `202` + `job_id` immediately; the run executes in a
dedicated worker process consuming a Redis-backed **arq** queue; the consumer
polls `GET /research/{job_id}`. API tier and worker tier are separate AKS
deployments sharing one image.

arq over alternatives: async-native (pairs with FastAPI without thread
bridges), Redis-only (no extra broker), built-in retries/timeout/uniqueness.
Redis Streams + consumer groups is an acceptable substitute if arq is ever
retired; the `JobQueue` protocol in `app/state/queue.py` is the seam.

## Consequences

- Long runs survive ingress limits; the consumer's LangGraph orchestrator
  polls at its own pace.
- Queue depth is an explicit KEDA autoscaling signal for the worker tier.
- A PAT can no longer stay request-scoped for USER_PAT domains — this forces
  the staging exception documented in ADR-0005.
- Polling adds latency (bounded by poll interval) — accepted; research is
  minutes-long anyway.
