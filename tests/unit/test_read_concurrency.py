from __future__ import annotations

import asyncio
import inspect

from app.worker.runner import cap_read_concurrency


def _make_tool(observed: dict, delay_s: float = 0.01):
    async def read_tool(query: str, max_results: int = 5) -> str:
        """A read-only tool."""
        observed["current"] += 1
        observed["peak"] = max(observed["peak"], observed["current"])
        await asyncio.sleep(delay_s)
        observed["current"] -= 1
        return query

    return read_tool


async def test_concurrent_calls_never_exceed_the_cap():
    observed = {"current": 0, "peak": 0}
    [tool] = cap_read_concurrency([_make_tool(observed)], limit=3)

    results = await asyncio.gather(*(tool(f"q{i}") for i in range(12)))
    assert results == [f"q{i}" for i in range(12)]  # all calls complete
    assert observed["peak"] == 3  # fully parallel up to the cap, never past it


async def test_cap_is_shared_across_all_tools_in_a_run():
    observed = {"current": 0, "peak": 0}
    tools = cap_read_concurrency([_make_tool(observed), _make_tool(observed)], limit=2)

    await asyncio.gather(*(tool(f"q{i}") for i in range(6) for tool in tools))
    assert observed["peak"] == 2


async def test_zero_limit_disables_the_cap():
    observed = {"current": 0, "peak": 0}
    [tool] = cap_read_concurrency([_make_tool(observed)], limit=0)

    await asyncio.gather(*(tool(f"q{i}") for i in range(8)))
    assert observed["peak"] == 8


def test_wrapper_preserves_tool_identity_for_schema_generation():
    async def search_issues(jql: str, max_results: int = 20) -> str:
        """Search Jira issues with a JQL query."""
        return jql

    [wrapped] = cap_read_concurrency([search_issues], limit=4)
    assert wrapped.__name__ == "search_issues"
    assert wrapped.__doc__ == "Search Jira issues with a JQL query."
    assert inspect.signature(wrapped) == inspect.signature(search_issues)
    assert inspect.iscoroutinefunction(wrapped)


def test_sync_tools_pass_through_unwrapped():
    def sync_tool(x: int) -> int:
        return x

    [passed] = cap_read_concurrency([sync_tool], limit=4)
    assert passed is sync_tool
