"""Contract: the two performance seams — model tiering and the per-run
read-concurrency cap — behave identically for every domain and never alter
the API contract."""

from __future__ import annotations

from app.adapters.base import DownstreamCredentials


async def test_fast_model_defaults_to_none(env):
    """Without a fast deployment configured, the harness runs single-model."""
    await env.research_completed()
    assert env.captured_factory_calls
    assert all(call["fast_model"] is None for call in env.captured_factory_calls)


async def test_fast_model_reaches_the_agent_factory(env):
    """With build_fast_llm wired, the factory receives the fast tier and the
    run completes with an unchanged contract."""
    sentinel = object()
    job_id = (await env.submit()).json()["job_id"]
    await env.run_worker(env.worker_deps_with(build_fast_llm=lambda settings: sentinel))

    assert env.captured_factory_calls[-1]["fast_model"] is sentinel
    body = (await env.poll(job_id)).json()
    assert body["status"] == "completed"


async def test_capped_tools_keep_identity_for_the_harness(env):
    """The concurrency wrapper must be invisible to the harness: same names,
    same docstrings, same signatures (the harness builds tool schemas from
    them), and still exactly the adapter's read-only set."""
    import inspect

    await env.research_completed()
    adapter = env.worker_deps.adapter
    originals = {t.__name__: t for t in adapter.read_tools(DownstreamCredentials())}

    for call in env.captured_factory_calls:
        bound = {t.__name__: t for t in call["tools"]}
        assert set(bound) == set(originals)
        for name, tool in bound.items():
            assert tool.__doc__ == originals[name].__doc__
            assert inspect.signature(tool) == inspect.signature(originals[name])
