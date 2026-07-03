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
    def __call__(self, *, model: Any, tools: list[Any], instructions: str) -> Any: ...


def deep_agent_factory(*, model: Any, tools: list[Any], instructions: str) -> Any:
    """Default harness: LangChain Deep Agents (planning + sub-agents +
    virtual filesystem built in). Version pins in pyproject.toml are exact."""
    from deepagents import create_deep_agent

    return create_deep_agent(model=model, tools=tools, system_prompt=instructions)


def react_agent_factory(*, model: Any, tools: list[Any], instructions: str) -> Any:
    """Alternative harness: plain LangChain agent. Registered to prove (and
    test) that the harness is swappable behind the same worker loop."""
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
