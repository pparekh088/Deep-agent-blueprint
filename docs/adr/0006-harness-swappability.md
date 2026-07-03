# ADR-0006: The agent harness is an implementation detail

**Status:** Accepted · **Applies to:** template core

## Context

We build on `deepagents` (LangChain Deep Agents) today. The agent-framework
ecosystem moves fast; harness churn must not become API churn for every
consumer, and a regulated environment demands exact, deliberate version
control.

## Decision

- The API contract never leaks harness concepts: no message objects, graph
  state, or checkpoint handles cross the HTTP boundary — only
  findings/proposed-action JSON.
- All harness construction goes through `app/agent/factory.py`. The worker
  depends only on the returned object's `astream(input,
  stream_mode="values")` protocol (LangGraph's runnable surface — shared by
  `create_deep_agent`, `create_agent`, and hand-built graphs).
- Swapping harness per domain = register a factory + set `AGENT_FACTORY`.
  A `react` factory (`langchain.agents.create_agent`) ships registered as
  proof.
- `deepagents`, `langchain`, `langgraph`, `langchain-openai`,
  `langchain-mcp-adapters` are pinned **exactly** in `pyproject.toml` and
  bumped only together, with the contract suite as the gate.

## Consequences

- The fake harness in the contract suite is just another factory — the whole
  service is testable with zero LLM calls, and the suite itself proves the
  swap seam works.
- We forgo harness-specific API features (e.g. LangGraph interrupts — see
  ADR-0003) at the boundary. Inside the worker, any harness capability is
  fair game.
- Version bumps are a template-core release (TEMPLATE_VERSION), back-ported
  to domain repos deliberately, never ad hoc per domain.
