"""Contract-suite fixtures: an in-process service (FastAPI app + fake queue +
fake agent harness + fakeredis) parameterized over every registered domain
case. No network, no Azure, no LLM — the contract is exercised end to end.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

import httpx
import pytest
from fakeredis import aioredis as fakeredis_aio

from app.config import Settings
from app.main import AppDeps, create_app
from app.observability.logging import build_formatter, clear_secrets
from app.state.redis_store import RedisStore
from app.state.token_vault import LocalTokenVault
from app.worker.runner import RetryableJobError, WorkerDeps, run_research_job
from tests.contract.cases import CASES, ContractCase

API_KEYS = {
    "consumer-a": "test-api-key-secret-aaaa1111",
    "consumer-b": "test-api-key-secret-bbbb2222",
}
LOCAL_KEY = base64.b64encode(b"0" * 32).decode()


class InMemoryQueue:
    """Queue double: records enqueued job ids; the Env drives the worker."""

    def __init__(self) -> None:
        self.jobs: list[str] = []

    async def enqueue_research(self, job_id: str) -> None:
        self.jobs.append(job_id)

    async def depth(self) -> int:
        return len(self.jobs)


class FakeAgent:
    """Stands in for the deep agent: same astream(values) protocol, canned
    final message. Hooks let tests inject failure/latency/cancellation."""

    def __init__(
        self,
        final_text: str,
        *,
        fail: bool = False,
        delay_s: float = 0.0,
        on_step: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._final_text = final_text
        self._fail = fail
        self._delay_s = delay_s
        self._on_step = on_step

    async def astream(self, agent_input: dict[str, Any], stream_mode: str = "values"):
        user_msg = agent_input["messages"][0]
        tool_msg = SimpleNamespace(content="", tool_calls=[{"name": "fake_read_tool", "args": {}}])
        yield {"messages": [user_msg, tool_msg]}
        if self._on_step is not None:
            await self._on_step()
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._fail:
            raise RuntimeError("synthetic downstream failure")
        final = SimpleNamespace(
            content=f"Research done.\n\n```json\n{self._final_text}\n```", tool_calls=[]
        )
        yield {"messages": [user_msg, tool_msg, final]}


@dataclass
class Env:
    case: ContractCase
    settings: Settings
    store: RedisStore
    queue: InMemoryQueue
    client: httpx.AsyncClient
    worker_deps: WorkerDeps
    captured_factory_calls: list[dict[str, Any]]
    log_lines: list[str] = field(default_factory=list)

    # ── request helpers ──────────────────────────────────────────────────────

    def headers(
        self,
        *,
        api_key: str | None = API_KEYS["consumer-a"],
        token: bool = True,
        correlation_id: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        if api_key is not None:
            headers["X-Api-Key"] = api_key
        if token and self.case.user_token is not None:
            headers["X-User-Token"] = self.case.user_token
        if correlation_id is not None:
            headers["X-Correlation-Id"] = correlation_id
        if extra:
            headers.update(extra)
        return headers

    async def submit(self, **kwargs: Any) -> httpx.Response:
        body = {"task": "Investigate the reported problem", "session_id": "sess-1"}
        body.update(kwargs.pop("body", {}))
        return await self.client.post("/research", json=body, headers=self.headers(**kwargs))

    async def poll(self, job_id: str, **kwargs: Any) -> httpx.Response:
        return await self.client.get(f"/research/{job_id}", headers=self.headers(**kwargs))

    async def cancel(self, job_id: str, **kwargs: Any) -> httpx.Response:
        return await self.client.delete(f"/research/{job_id}", headers=self.headers(**kwargs))

    async def execute(
        self, action_id: str, payload: dict[str, Any], idem_key: str, **kwargs: Any
    ) -> httpx.Response:
        headers = self.headers(**kwargs)
        headers["Idempotency-Key"] = idem_key
        return await self.client.post(
            "/execute",
            json={
                "action_id": action_id,
                "session_id": "sess-1",
                "approved_payload": payload,
                "approval": {"approved_by": "user@corp", "approved_at": "2026-07-03T10:00:00Z"},
            },
            headers=headers,
        )

    # ── worker helpers ───────────────────────────────────────────────────────

    async def run_worker(self, worker_deps: WorkerDeps | None = None) -> None:
        """Drain the queue exactly as arq would: attempts + redelivery."""
        deps = worker_deps or self.worker_deps
        while self.queue.jobs:
            job_id = self.queue.jobs.pop(0)
            for attempt in range(1, deps.settings.job_max_attempts + 1):
                try:
                    await run_research_job(deps, job_id, attempt)
                    break
                except RetryableJobError:
                    continue

    async def research_completed(self) -> dict[str, Any]:
        """submit -> worker -> poll; returns the completed poll body."""
        submitted = await self.submit()
        assert submitted.status_code == 202, submitted.text
        job_id = submitted.json()["job_id"]
        await self.run_worker()
        polled = await self.poll(job_id)
        assert polled.status_code == 200, polled.text
        return polled.json()

    def agent_factory_with(self, **agent_kwargs: Any) -> Callable[..., FakeAgent]:
        final_text = json.dumps(self.case.agent_output)

        def factory(
            *, model: Any, tools: list[Any], instructions: str, fast_model: Any | None = None
        ) -> FakeAgent:
            self.captured_factory_calls.append(
                {
                    "tools": tools,
                    "instructions": instructions,
                    "model": model,
                    "fast_model": fast_model,
                }
            )
            return FakeAgent(final_text, **agent_kwargs)

        return factory

    def worker_deps_with(self, **overrides: Any) -> WorkerDeps:
        return replace(self.worker_deps, **overrides)


@pytest.fixture(params=CASES, ids=lambda case_builder: case_builder.__name__.strip("_"))
def case(request: pytest.FixtureRequest) -> ContractCase:
    return request.param()


@pytest.fixture
async def env(case: ContractCase) -> Any:
    clear_secrets()
    settings = Settings(
        _env_file=None,
        domain=case.name,
        env="test",
        api_keys=API_KEYS,
        local_crypto_key_b64=LOCAL_KEY,
        principal_hash_salt="contract-test-salt",
        job_max_attempts=2,
        **case.settings_overrides,
    )
    redis = fakeredis_aio.FakeRedis(decode_responses=True)
    store = RedisStore(redis, settings)
    vault = LocalTokenVault(base64.b64decode(LOCAL_KEY))
    adapter = case.build()
    queue = InMemoryQueue()

    app = create_app(AppDeps(settings=settings, store=store, vault=vault, adapter=adapter, queue=queue))

    captured: list[dict[str, Any]] = []
    environment = Env(
        case=case,
        settings=settings,
        store=store,
        queue=queue,
        client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://svc"
        ),
        worker_deps=WorkerDeps(
            settings=settings,
            store=store,
            vault=vault,
            adapter=adapter,
            agent_factory=lambda **kw: (_ for _ in ()).throw(RuntimeError("factory unset")),
            build_llm=lambda s: None,
        ),
        captured_factory_calls=captured,
    )
    environment.worker_deps = replace(
        environment.worker_deps, agent_factory=environment.agent_factory_with()
    )

    # Capture every redacted, formatted log line the service emits.
    formatter = build_formatter(settings)
    lines = environment.log_lines

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(formatter.format(record))

    handler = _Capture()
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    try:
        yield environment
    finally:
        root.removeHandler(handler)
        await environment.client.aclose()
        await redis.aclose()
        clear_secrets()
