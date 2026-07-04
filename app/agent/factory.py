"""TEMPLATE_CORE — swappable agent factory.

The harness is an implementation detail (ADR-0006). The rest of the service
only depends on the returned object exposing LangGraph's ``astream(input,
stream_mode="values")`` protocol, where states carry a ``messages`` list.
Swapping Deep Agents for ``langchain.agents.create_agent`` or a hand-built
LangGraph graph is: write a factory, register it, set AGENT_FACTORY — zero
API contract change.

deepagents / langchain / langgraph are imported lazily inside the factory so
the API tier (which never runs an agent) does not pay the import, and so the
harness can be swapped without dead imports.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol


class AgentFactory(Protocol):
    def __call__(
        self, *, model: Any, tools: list[Any], instructions: str, fast_model: Any | None = None
    ) -> Any: ...


# Model tiering: quality lives in planning/synthesis/proposals (main agent,
# primary model); retrieval + per-source summarization is mostly extraction
# and tolerates a cheaper, faster model. When a fast deployment is configured
# the deep agent gets a "retriever" sub-agent on that tier; the main agent
# delegates independent lookups to it in parallel.
_RETRIEVER_DESCRIPTION = (
    "Fast retrieval specialist. Delegate independent lookups here — one "
    "sub-task per source or entity, launched in parallel — to fetch raw data "
    "and return concise per-source summaries. Keep planning, cross-source "
    "synthesis, and action proposals in the main agent."
)
_RETRIEVER_PROMPT = (
    "You retrieve and summarize; you do not plan or propose actions. Use the "
    "read-only tools to fetch exactly what the sub-task asks for, then reply "
    "with a compact structured summary: key facts, identifiers/URLs suitable "
    "for citation, and the target's current state (status/version/timestamps "
    "— the caller needs these for action preconditions). No speculation. "
    "Never include credentials or secrets."
)


def deep_agent_factory(
    *, model: Any, tools: list[Any], instructions: str, fast_model: Any | None = None
) -> Any:
    """Default harness: LangChain Deep Agents (planning + sub-agents +
    virtual filesystem built in). Version pins in pyproject.toml are exact."""
    from deepagents import create_deep_agent

    subagents = None
    if fast_model is not None:
        subagents = [
            {
                "name": "retriever",
                "description": _RETRIEVER_DESCRIPTION,
                "system_prompt": _RETRIEVER_PROMPT,
                "tools": tools,
                "model": fast_model,
            }
        ]
    return create_deep_agent(
        model=model, tools=tools, system_prompt=instructions, subagents=subagents
    )


def react_agent_factory(
    *, model: Any, tools: list[Any], instructions: str, fast_model: Any | None = None
) -> Any:
    """Alternative harness: plain LangChain agent. Registered to prove (and
    test) that the harness is swappable behind the same worker loop.
    Single-model by construction — ``fast_model`` has no seat here and is
    deliberately ignored."""
    from langchain.agents import create_agent

    return create_agent(model=model, tools=tools, system_prompt=instructions)


_FACTORIES: dict[str, AgentFactory] = {
    "deepagents": deep_agent_factory,
    "react": react_agent_factory,
}


def register_agent_factory(name: str, factory: AgentFactory) -> None:
    _FACTORIES[name] = factory


def get_agent_factory(name: str) -> AgentFactory:
    try:
        return _FACTORIES[name]
    except KeyError:
        raise ValueError(
            f"Unknown AGENT_FACTORY '{name}'. Registered: {sorted(_FACTORIES)}"
        ) from None
