import logging
import time

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


def _fast_stamina(monkeypatch):
    """Zero the (real) stamina retry loop's waits, per the fast_retry_context
    idiom in test_state_manager.py — keeps a persistently-failing retry policy
    from actually sleeping through SLOT_REDIS_RETRY's wait_initial/wait_max
    while still exercising the real retry_context (`on=redis.RedisError` must
    match a real RedisError instance, not a mock)."""
    import stamina

    real_retry_context = stamina.retry_context

    def fast_retry_context(*args, **kwargs):
        kwargs["wait_initial"] = 0
        kwargs["wait_max"] = 0
        kwargs["wait_jitter"] = 0
        return real_retry_context(*args, **kwargs)

    monkeypatch.setattr(stamina, "retry_context", fast_retry_context)


@pytest.mark.asyncio
async def test_slot_retry_window_is_floored_not_the_wait_deadline_on_first_pass(
    fake_redis, monkeypatch
):
    """max_wait_seconds is the SATURATION-queueing budget, not a Redis-error
    budget (Fix 1): a Redis brownout on the first acquire pass must not be cut
    short to the caller's tiny queueing deadline — it floors to
    SLOT_REDIS_RETRY_FLOOR so the caller still gets the full SLOT_REDIS_RETRY
    policy instead of losing it to a budget it was never meant to share with."""
    from redis.exceptions import RedisError
    from app.services.lotek_connections import SLOT_REDIS_RETRY

    _fast_stamina(monkeypatch)
    fake_redis.eval = AsyncMock(side_effect=RedisError("brownout"))

    start = time.monotonic()
    with pytest.raises(NoConnectionSlot):
        async with lotek_slot("user@example.com", max_wait_seconds=0.05):
            pytest.fail("body must not run")
    elapsed = time.monotonic() - start

    assert fake_redis.eval.await_count == SLOT_REDIS_RETRY["attempts"]
    # Waits are zeroed above, so this pins the test's own speed, not a
    # production bound (the floor itself is ~20s of *allowance*, not sleep).
    assert elapsed < 1.0
    fake_redis.zrem.assert_awaited_once()


@pytest.mark.asyncio
async def test_slot_retry_gets_the_full_retry_policy_with_zero_wait_budget(
    fake_redis, monkeypatch
):
    """A caller with no wait budget at all (the common default — the fail-fast
    dispatcher) must still get the full SLOT_REDIS_RETRY policy on a Redis
    error, not one unretried attempt: losing the shared retry policy here is
    exactly how a Redis brownout escaped as a fabricated per-device Lotek
    failure (D5, Fix 1)."""
    from redis.exceptions import RedisError
    from app.services.lotek_connections import SLOT_REDIS_RETRY

    _fast_stamina(monkeypatch)
    fake_redis.eval = AsyncMock(side_effect=RedisError("brownout"))

    with pytest.raises(NoConnectionSlot):
        async with lotek_slot("user@example.com"):
            pytest.fail("body must not run")

    assert fake_redis.eval.await_count == SLOT_REDIS_RETRY["attempts"]
    fake_redis.zrem.assert_awaited_once()


@pytest.mark.asyncio
async def test_fail_fast_caller_is_actually_retried_on_a_redis_brownout(
    fake_redis, monkeypatch
):
    """The D5 sentinel test above only pins SLOT_REDIS_RETRY's value, so it
    stayed green while the dispatcher's fail-fast caller silently lost its
    retries to the queueing-budget bound (Fix 1's regression) — the constant
    was untouched. Pin the retry actually helping: two transient RedisErrors
    then a grant, with max_wait_seconds=0 (the dispatcher's own default),
    must still succeed via more than one eval attempt."""
    from redis.exceptions import RedisError

    _fast_stamina(monkeypatch)
    fake_redis.eval = AsyncMock(side_effect=[RedisError("blip"), RedisError("blip"), 1])

    async with lotek_slot("user@example.com"):
        pass  # must not raise: the third attempt granted the slot

    assert fake_redis.eval.await_count > 1


@pytest.mark.asyncio
async def test_give_up_on_redis_error_chains_the_cause_and_warns(fake_redis, monkeypatch, caplog):
    """Converting a Redis failure into NoConnectionSlot is correct (it keeps
    per-device deferral and the config_data credential-leak route closed), but
    it must stay diagnosable: the original RedisError is chained (`raise ...
    from exc`) and named at WARNING, so a Redis-caused give-up is never
    mistaken for the "connection budget exhausted" diagnosis genuine
    saturation gets (Fix 2)."""
    from redis.exceptions import RedisError
    _fast_stamina(monkeypatch)
    fake_redis.eval = AsyncMock(side_effect=RedisError("brownout"))

    with caplog.at_level(logging.WARNING):
        with pytest.raises(NoConnectionSlot) as exc_info:
            async with lotek_slot("user@example.com"):
                pytest.fail("body must not run")

    assert isinstance(exc_info.value.__cause__, RedisError)
    assert any("Redis" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_give_up_on_genuine_saturation_has_no_chained_cause(fake_redis, monkeypatch):
    """The other side of Fix 2: give-up on ordinary account saturation (the
    server said "no slot", no Redis exception at all) must NOT get a chained
    cause or the Redis-blamed warning — that diagnosis is reserved for an
    actual Redis-side failure."""
    monkeypatch.setattr(lc, "SLOT_WAIT_POLL_INITIAL", 0)
    monkeypatch.setattr(lc, "SLOT_WAIT_POLL_MAX", 0)
    monkeypatch.setattr(lc, "SLOT_WAIT_JITTER", 0)
    fake_redis.eval = AsyncMock(return_value=0)

    with pytest.raises(NoConnectionSlot) as exc_info:
        async with lotek_slot("user@example.com", max_wait_seconds=0.05):
            pytest.fail("body must not run")

    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_budget_expiry_during_the_final_sleep_is_still_plain_saturation(
    fake_redis, monkeypatch
):
    """The saturation sleep is bounded by `remaining`, so the final sleep can
    consume the whole queueing budget. The next pass then computed
    wait_for(timeout<=0), which raises TimeoutError before any Redis call runs
    — misreporting ordinary saturation as a Redis failure with a chained cause
    (Copilot review, round 4). An expired window must instead make one last
    unretried attempt (the sleep existed to wait for a peer to release) and
    then give up cause-free."""
    # Real (small) pause constants so expiry happens via the sleep, exactly
    # as in production — NOT zeroed, which would busy-spin instead.
    monkeypatch.setattr(lc, "SLOT_WAIT_POLL_INITIAL", 0.05)
    monkeypatch.setattr(lc, "SLOT_WAIT_POLL_MAX", 0.05)
    monkeypatch.setattr(lc, "SLOT_WAIT_JITTER", 0)
    fake_redis.eval = AsyncMock(return_value=0)

    with pytest.raises(NoConnectionSlot) as exc_info:
        async with lotek_slot("user@example.com", max_wait_seconds=0.02):
            pytest.fail("body must not run")

    # Cause-free: the server answered "at capacity" every time; no Redis call
    # failed, so nothing may be chained or blamed on Redis.
    assert exc_info.value.__cause__ is None
    # The post-sleep final attempt actually happened: first pass plus one
    # last check after the budget-consuming sleep.
    assert fake_redis.eval.await_count == 2
    fake_redis.zrem.assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_caused_give_up_raises_the_distinct_backend_type(
    fake_redis, monkeypatch
):
    """Redis retry exhaustion must be distinguishable BY TYPE from account
    saturation (Copilot review, round 5): every caller treated NoConnectionSlot
    as genuine saturation, so during a persistent Redis outage pulls reported
    as clean capacity deferrals and never moved the portal health signal.
    SlotBackendUnavailable subclasses NoConnectionSlot so an unaware caller
    still degrades to the safe non-raising path rather than crashing."""
    from redis.exceptions import RedisError
    from app.services.lotek_connections import SlotBackendUnavailable

    _fast_stamina(monkeypatch)
    fake_redis.eval = AsyncMock(side_effect=RedisError("brownout"))

    with pytest.raises(SlotBackendUnavailable) as exc_info:
        async with lotek_slot("user@example.com"):
            pytest.fail("body must not run")

    assert isinstance(exc_info.value, NoConnectionSlot)  # safe-fallback contract
    assert isinstance(exc_info.value.__cause__, RedisError)


@pytest.mark.asyncio
async def test_genuine_saturation_is_never_the_backend_type(fake_redis, monkeypatch):
    """The server answering "at capacity" is saturation, not a backend
    failure — it must stay the plain NoConnectionSlot so callers keep the
    quiet capacity-deferral path."""
    from app.services.lotek_connections import SlotBackendUnavailable

    fake_redis.eval = AsyncMock(return_value=0)

    with pytest.raises(NoConnectionSlot) as exc_info:
        async with lotek_slot("user@example.com"):
            pytest.fail("body must not run")

    assert not isinstance(exc_info.value, SlotBackendUnavailable)


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
