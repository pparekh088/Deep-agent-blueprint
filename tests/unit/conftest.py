from __future__ import annotations

import base64

import pytest
from fakeredis import aioredis as fakeredis_aio

from app.config import Settings
from app.state.redis_store import RedisStore


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        domain="jira",
        env="test",
        api_keys={"consumer-a": "unit-test-key-secret-1"},
        local_crypto_key_b64=base64.b64encode(b"1" * 32).decode(),
        principal_hash_salt="unit-salt",
    )


@pytest.fixture
async def store(settings) -> RedisStore:
    redis = fakeredis_aio.FakeRedis(decode_responses=True)
    yield RedisStore(redis, settings)
    await redis.aclose()
