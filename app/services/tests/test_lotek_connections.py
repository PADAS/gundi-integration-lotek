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
    # ...and the exact member added is the one removed on release. (ARGV order:
    # now, expiry, ceiling, token, key_ttl — the token is second from the end.)
    token = args[-2]
    fake_redis.zrem.assert_awaited_once_with(connection_key("user@example.com"), token)


@pytest.mark.asyncio
async def test_acquire_at_capacity_raises_and_best_effort_releases(fake_redis):
    # Give-up (including the fail-fast default with no wait budget) cannot
    # tell "server said 0" apart from "granted but the reply was lost", so it
    # always attempts a cleanup zrem before raising — a no-op if the token
    # was never actually added.
    fake_redis.eval = AsyncMock(return_value=0)
    with pytest.raises(NoConnectionSlot):
        async with lotek_slot("user@example.com"):
            pytest.fail("body must not run when at capacity")
    fake_redis.zrem.assert_awaited_once()


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
    # ARGV: now, expiry, ceiling, token, key_ttl (after script + numkeys + key)
    now_arg, expiry_arg, ceiling_arg = args[3], args[4], args[5]
    assert before <= now_arg <= time.time()
    assert expiry_arg == pytest.approx(now_arg + 300)
    from app import settings
    assert ceiling_arg == settings.LOTEK_MAX_CONNECTIONS
    # The key's own TTL must outlive the longest-lived member, so a refresh can
    # never expire a key that still has live holders.
    assert args[7] > 300


def test_connection_key_is_stable_and_username_scoped():
    assert connection_key("a") == connection_key("a")
    assert connection_key("a") != connection_key("b")
    assert connection_key("a").startswith("lotek:connections:")


@pytest.mark.asyncio
async def test_slot_retry_policy_matches_state_manager(fake_redis):
    """A Redis brownout that every IntegrationStateManager op survives must not
    escape from the slot acquire as a fake per-device Lotek failure. The policy
    here has to match state.py's, not undercut it.
    """
    from app.services.lotek_connections import SLOT_REDIS_RETRY

    assert SLOT_REDIS_RETRY == {
        "attempts": 5, "wait_initial": 1.0, "wait_max": 30, "wait_jitter": 3.0,
    }


@pytest.mark.asyncio
async def test_reacquire_with_same_token_is_idempotent(fake_redis):
    """A lost reply on the acquire that took the last slot must not refuse the
    caller that already owns that slot. The Lua checks ZSCORE for the token
    before the ZCARD ceiling test, so a retry with the same token re-grants.

    Simulated at the script level: the real guarantee lives in the Lua, so this
    test pins that the script text contains the membership fast-path ahead of
    the capacity check.
    """
    from app.services.lotek_connections import _ACQUIRE_LUA

    zscore_at = _ACQUIRE_LUA.find("ZSCORE")
    zcard_at = _ACQUIRE_LUA.find("ZCARD")
    assert zscore_at != -1, "acquire must check token membership before capacity"
    assert zscore_at < zcard_at, "membership fast-path must precede the ceiling test"
    # The fast-path must re-arm the member's expiry rather than returning a
    # stale score, so a waiting retry cannot inherit an about-to-expire slot.
    assert "ZADD" in _ACQUIRE_LUA[zscore_at:zcard_at]


def test_expire_runs_after_every_zadd_and_before_every_return():
    """A key this script's own ZADD just created, or that a concurrent
    holder's ZADD created and this call merely found at capacity, must get a
    TTL before the script returns 0 or 1 — otherwise the very first acquire
    on a brand-new account key leaves a persistent zset with no TTL at all,
    only fixed up whenever some later acquire happens to run EXPIRE (review
    finding: EXPIRE used to run once, before either branch, so it covered the
    at-capacity return-0 path but missed the TTL-less key its own ZADD had
    just created on the create path).

    The `fake_redis` AsyncMock can't model key creation, so this pins the
    guarantee at the script-text level, in the style of
    test_reacquire_with_same_token_is_idempotent: scanning top to bottom,
    every `return` must be preceded by an EXPIRE since the most recent ZADD
    (or since the start of the script, for the return that has no ZADD at
    all on its path).
    """
    from app.services.lotek_connections import _ACQUIRE_LUA

    lines = [ln.strip() for ln in _ACQUIRE_LUA.strip().splitlines()]
    expired_since_last_zadd = True  # true at the top, before any ZADD
    for line in lines:
        if line.startswith("redis.call('ZADD'"):
            expired_since_last_zadd = False
        if line.startswith("redis.call('EXPIRE'"):
            expired_since_last_zadd = True
        if line.startswith("return"):
            assert expired_since_last_zadd, (
                f"{line!r} is reachable without an EXPIRE since the last "
                f"ZADD (or since the start of the script) — the key can be "
                f"left with no TTL."
            )


@pytest.mark.asyncio
async def test_slot_waits_then_acquires_when_capacity_frees(fake_redis, monkeypatch):
    """Oversubscription must queue, not refuse: with shards x FETCH_CONCURRENCY
    well above the ceiling, a caller that waits a moment gets a slot instead of
    deferring its whole tail (spec D2)."""
    monkeypatch.setattr(lc, "SLOT_WAIT_POLL_INITIAL", 0)
    monkeypatch.setattr(lc, "SLOT_WAIT_POLL_MAX", 0)
    monkeypatch.setattr(lc, "SLOT_WAIT_JITTER", 0)
    # Refused twice (account saturated), then a peer releases.
    fake_redis.eval = AsyncMock(side_effect=[0, 0, 1])

    async with lotek_slot("user@example.com", max_wait_seconds=5.0):
        pass

    assert fake_redis.eval.await_count == 3
    fake_redis.zrem.assert_awaited_once()


@pytest.mark.asyncio
async def test_slot_gives_up_when_wait_budget_exhausted(fake_redis, monkeypatch):
    """Waiting must never eat the caller's action deadline: once the budget is
    spent the slot raises, and the caller defers as before."""
    monkeypatch.setattr(lc, "SLOT_WAIT_POLL_INITIAL", 0)
    monkeypatch.setattr(lc, "SLOT_WAIT_POLL_MAX", 0)
    monkeypatch.setattr(lc, "SLOT_WAIT_JITTER", 0)
    fake_redis.eval = AsyncMock(return_value=0)

    with pytest.raises(NoConnectionSlot):
        async with lotek_slot("user@example.com", max_wait_seconds=0.05):
            pytest.fail("body must not run when the account stays saturated")

    assert fake_redis.eval.await_count >= 1
    # Best-effort cleanup on give-up: we cannot tell "server said 0" apart
    # from "server granted it but the reply was lost" from the client side,
    # so the give-up path always attempts to remove the token. It is a no-op
    # if the token was never actually added.
    fake_redis.zrem.assert_awaited_once()


@pytest.mark.asyncio
async def test_slot_give_up_releases_a_token_the_server_actually_granted(fake_redis, monkeypatch):
    """If the final poll's reply is lost after the server already granted the
    slot (e.g. a network blip on the way back), the give-up path must not
    strand that slot for the full TTL: it removes the token before raising."""
    monkeypatch.setattr(lc, "SLOT_WAIT_POLL_INITIAL", 0)
    monkeypatch.setattr(lc, "SLOT_WAIT_POLL_MAX", 0)
    monkeypatch.setattr(lc, "SLOT_WAIT_JITTER", 0)
    fake_redis.eval = AsyncMock(return_value=0)

    with pytest.raises(NoConnectionSlot):
        async with lotek_slot("user@example.com", max_wait_seconds=0.05):
            pytest.fail("body must not run when the account stays saturated")

    # The token removed is the same one used in every acquire attempt.
    token = fake_redis.eval.await_args.args[-2]
    fake_redis.zrem.assert_awaited_once_with(connection_key("user@example.com"), token)


@pytest.mark.asyncio
async def test_slot_give_up_zrem_failure_does_not_mask_no_connection_slot(fake_redis, monkeypatch):
    """A failed best-effort cleanup on give-up must not turn into some other
    exception that hides the real NoConnectionSlot from the caller."""
    monkeypatch.setattr(lc, "SLOT_WAIT_POLL_INITIAL", 0)
    monkeypatch.setattr(lc, "SLOT_WAIT_POLL_MAX", 0)
    monkeypatch.setattr(lc, "SLOT_WAIT_JITTER", 0)
    fake_redis.eval = AsyncMock(return_value=0)
    fake_redis.zrem = AsyncMock(side_effect=ConnectionError("redis blip"))

    with pytest.raises(NoConnectionSlot):
        async with lotek_slot("user@example.com", max_wait_seconds=0.05):
            pytest.fail("body must not run when the account stays saturated")


@pytest.mark.asyncio
async def test_slot_without_wait_budget_still_fails_fast(fake_redis):
    """Default stays fail-fast for callers with no deadline to reason about."""
    fake_redis.eval = AsyncMock(return_value=0)
    with pytest.raises(NoConnectionSlot):
        async with lotek_slot("user@example.com"):
            pytest.fail("body must not run")
    assert fake_redis.eval.await_count == 1


@pytest.mark.asyncio
async def test_close_connection_client_closes_and_resets(monkeypatch):
    # The FastAPI lifespan closes every other module-level client; without this
    # the pooled connections are reclaimed by __del__ after the loop is gone,
    # logging "Event loop is closed" per connection on each restart.
    from app.services.lotek_connections import close_connection_client

    client = AsyncMock()
    monkeypatch.setattr(lc, "_shared_client", client)
    await close_connection_client()
    client.aclose.assert_awaited_once()
    assert lc._shared_client is None
    await close_connection_client()  # idempotent: no client, no error
