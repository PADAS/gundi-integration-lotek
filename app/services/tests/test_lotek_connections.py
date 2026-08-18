import pytest
from unittest.mock import AsyncMock

import app.services.lotek_connections as lc
from app.services.lotek_connections import NoConnectionSlot, connection_key, lotek_slot


@pytest.fixture
def fake_redis(monkeypatch):
    """Deterministic stand-in for the module's shared Redis client: `eval`
    grants or refuses per test wiring, `zrem` records releases."""
    client = AsyncMock()
    client.eval = AsyncMock(return_value=1)
    client.zrem = AsyncMock(return_value=1)
    monkeypatch.setattr(lc, "_shared_client", client)
    yield client
    monkeypatch.setattr(lc, "_shared_client", None)


@pytest.mark.asyncio
async def test_acquire_under_capacity_grants_and_releases(fake_redis):
    async with lotek_slot("user@example.com"):
        pass
    # One atomic Lua acquire against the per-username key...
    assert fake_redis.eval.await_count == 1
    args = fake_redis.eval.await_args.args
    assert args[2] == connection_key("user@example.com")
    # ...and the exact member added is the one removed on release.
    token = args[-1]
    fake_redis.zrem.assert_awaited_once_with(connection_key("user@example.com"), token)


@pytest.mark.asyncio
async def test_acquire_at_capacity_raises_and_does_not_release(fake_redis):
    fake_redis.eval = AsyncMock(return_value=0)
    with pytest.raises(NoConnectionSlot):
        async with lotek_slot("user@example.com"):
            pytest.fail("body must not run when at capacity")
    fake_redis.zrem.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_happens_even_when_body_raises(fake_redis):
    with pytest.raises(RuntimeError):
        async with lotek_slot("user@example.com"):
            raise RuntimeError("request blew up")
    fake_redis.zrem.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_release_is_swallowed(fake_redis):
    # A zrem blip must not turn a successful Lotek call into an action
    # failure; the slot self-expires via its TTL score.
    fake_redis.zrem = AsyncMock(side_effect=ConnectionError("redis blip"))
    async with lotek_slot("user@example.com"):
        pass  # must not raise


@pytest.mark.asyncio
async def test_expiry_score_and_ceiling_are_passed_to_the_lua_script(fake_redis):
    import time
    before = time.time()
    async with lotek_slot("user@example.com", ttl_seconds=300):
        pass
    args = fake_redis.eval.await_args.args
    # ARGV: now, expiry, ceiling, token (after script + numkeys + key)
    now_arg, expiry_arg, ceiling_arg = args[3], args[4], args[5]
    assert before <= now_arg <= time.time()
    assert expiry_arg == pytest.approx(now_arg + 300)
    from app import settings
    assert ceiling_arg == settings.LOTEK_MAX_CONNECTIONS


def test_connection_key_is_stable_and_username_scoped():
    assert connection_key("a") == connection_key("a")
    assert connection_key("a") != connection_key("b")
    assert connection_key("a").startswith("lotek:connections:")
