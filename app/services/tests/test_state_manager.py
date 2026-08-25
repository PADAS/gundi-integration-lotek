import datetime
import json

import pytest
import stamina
from app.conftest import async_return
from app.services.state import (
    IntegrationStateManager,
    _ACQUIRE_LEASE_SCRIPT,
    _INCREMENT_COUNTER_SCRIPT,
    _MERGE_STATE_SCRIPT,
    _RELEASE_LEASE_SCRIPT,
)
from ._lua_lite import FakeRedis, LuaError, run_script


def _fast_stamina(monkeypatch):
    """Zero the (real) stamina retry loop's waits, per the fast_retry_context
    idiom this module lends its name to (see test_lotek_connections.py) —
    keeps a real retry_context from actually sleeping through
    wait_initial/wait_max while still exercising it for real (`on=redis.
    RedisError` must match a real RedisError instance, not a mock)."""
    real_retry_context = stamina.retry_context

    def fast_retry_context(*args, **kwargs):
        kwargs["wait_initial"] = 0
        kwargs["wait_max"] = 0
        kwargs["wait_jitter"] = 0
        return real_retry_context(*args, **kwargs)

    monkeypatch.setattr(stamina, "retry_context", fast_retry_context)


@pytest.mark.asyncio
async def test_set_integration_state(mocker, mock_redis, integration_v2):
    mocker.patch("app.services.state.redis", mock_redis)
    state_manager = IntegrationStateManager()
    execution_timestamp = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()
    integration_id = str(integration_v2.id)
    state = {"last_execution": execution_timestamp}

    await state_manager.set_state(
        integration_id=integration_id,
        action_id="pull_observations",
        # No source set
        state=state
    )

    mock_redis.Redis.return_value.set.assert_called_once_with(
        f"integration_state.{integration_id}.pull_observations.no-source",
        '{"last_execution": "' + execution_timestamp + '"}'
    )


@pytest.mark.asyncio
async def test_get_integration_state(mocker, mock_redis, integration_v2, mock_integration_state):
    mocker.patch("app.services.state.redis", mock_redis)
    state_manager = IntegrationStateManager()
    integration_id = str(integration_v2.id)

    state = await state_manager.get_state(
        integration_id=integration_id,
        action_id="pull_observations",
        # No source set
    )

    assert state == mock_integration_state
    mock_redis.Redis.return_value.get.assert_called_once_with(
        f"integration_state.{integration_id}.pull_observations.no-source"
    )


@pytest.mark.asyncio
async def test_delete_integration_state(mocker, mock_redis, integration_v2):
    mocker.patch("app.services.state.redis", mock_redis)
    state_manager = IntegrationStateManager()

    execution_timestamp = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()
    integration_id = str(integration_v2.id)

    # set state
    state = {"last_execution": execution_timestamp}

    await state_manager.set_state(
        integration_id=integration_id,
        action_id="pull_observations",
        # No source set
        state=state
    )

    mock_redis.Redis.return_value.set.assert_called_once_with(
        f"integration_state.{integration_id}.pull_observations.no-source",
        '{"last_execution": "' + execution_timestamp + '"}'
    )

    # then delete the state

    await state_manager.delete_state(
        integration_id=integration_id,
        action_id="pull_observations",
        # No source set
    )

    mock_redis.Redis.return_value.delete.assert_called_once_with(
        f"integration_state.{integration_id}.pull_observations.no-source"
    )


@pytest.mark.asyncio
async def test_set_if_absent(mocker, mock_redis, integration_v2):
    mocker.patch("app.services.state.redis", mock_redis)
    state_manager = IntegrationStateManager()
    integration_id = str(integration_v2.id)

    # Redis SET ... NX EX returns a truthy value when the key was absent (set),
    # and None when it already existed (not set / throttled).
    mock_redis.Redis.return_value.set.return_value = async_return("OK")
    was_set = await state_manager.set_if_absent(
        integration_id=integration_id,
        action_id="pull_observations",
        source_id="skip-invalid-config-warning",
        ttl_seconds=3600,
    )
    assert was_set is True
    mock_redis.Redis.return_value.set.assert_called_once_with(
        f"integration_state.{integration_id}.pull_observations.skip-invalid-config-warning",
        "1",
        ex=3600,
        nx=True,
    )

    # Key already present within the window → Redis returns None → False.
    mock_redis.Redis.return_value.set.return_value = async_return(None)
    was_set = await state_manager.set_if_absent(
        integration_id=integration_id,
        action_id="pull_observations",
        source_id="skip-invalid-config-warning",
        ttl_seconds=3600,
    )
    assert was_set is False


@pytest.mark.asyncio
async def test_merge_state_fields_executes_atomic_lua(mocker, mock_redis, integration_v2):
    mocker.patch("app.services.state.redis", mock_redis)
    mock_redis.Redis.return_value.eval.return_value = async_return(1)
    state_manager = IntegrationStateManager()
    integration_id = str(integration_v2.id)
    updates = {"high_water": "2026-08-14T01:02:03+00:00", "gap_start": None}

    await state_manager.merge_state_fields(
        integration_id=integration_id,
        action_id="pull_observations",
        source_id="device-123",
        updates=updates,
    )

    mock_redis.Redis.return_value.eval.assert_called_once_with(
        _MERGE_STATE_SCRIPT,
        1,
        f"integration_state.{integration_id}.pull_observations.device-123",
        json.dumps(updates, default=str),
        json.dumps({}, default=str),
    )


@pytest.mark.asyncio
async def test_merge_state_fields_passes_init_only_fields(mocker, mock_redis, integration_v2):
    mocker.patch("app.services.state.redis", mock_redis)
    mock_redis.Redis.return_value.eval.return_value = async_return(1)
    state_manager = IntegrationStateManager()
    integration_id = str(integration_v2.id)
    updates = {"high_water": "2026-08-14T01:02:03+00:00"}
    init_only = {"gap_start": "2026-08-01T00:00:00+00:00", "gap_end": "2026-08-10T00:00:00+00:00"}

    await state_manager.merge_state_fields(
        integration_id=integration_id,
        action_id="pull_observations",
        source_id="device-123",
        updates=updates,
        init_only=init_only,
    )

    mock_redis.Redis.return_value.eval.assert_called_once_with(
        _MERGE_STATE_SCRIPT,
        1,
        f"integration_state.{integration_id}.pull_observations.device-123",
        json.dumps(updates, default=str),
        json.dumps(init_only, default=str),
    )


@pytest.mark.asyncio
async def test_set_source_state(mocker, mock_redis, integration_v2, mock_integration_state):
    mocker.patch("app.services.state.redis", mock_redis)
    state_manager = IntegrationStateManager()
    integration_id = str(integration_v2.id)
    source_id = "device-123"

    await state_manager.set_state(
        integration_id=integration_id,
        action_id="pull_observations",
        source_id=source_id,
        state=mock_integration_state
    )

    mock_redis.Redis.return_value.set.assert_called_once_with(
        f"integration_state.{integration_id}.pull_observations.{source_id}",
        json.dumps(mock_integration_state, default=str)
    )


@pytest.mark.asyncio
async def test_get_state_source_state(mocker, mock_redis, integration_v2, mock_integration_state):
    mocker.patch("app.services.state.redis", mock_redis)
    state_manager = IntegrationStateManager()
    integration_id = str(integration_v2.id)
    source_id = "device-123"

    state = await state_manager.get_state(
        integration_id=integration_id,
        action_id="pull_observations",
        source_id=source_id
    )

    assert state == mock_integration_state
    mock_redis.Redis.return_value.get.assert_called_once_with(
        f"integration_state.{integration_id}.pull_observations.{source_id}"
    )


@pytest.mark.asyncio
async def test_delete_state_source_state(mocker, mock_redis, integration_v2, mock_integration_state):
    mocker.patch("app.services.state.redis", mock_redis)
    state_manager = IntegrationStateManager()
    integration_id = str(integration_v2.id)
    source_id = "device-123"

    # set state
    await state_manager.set_state(
        integration_id=integration_id,
        action_id="pull_observations",
        source_id=source_id,
        state=mock_integration_state
    )

    mock_redis.Redis.return_value.set.assert_called_once_with(
        f"integration_state.{integration_id}.pull_observations.{source_id}",
        json.dumps(mock_integration_state, default=str)
    )

    # delete state

    await state_manager.delete_state(
        integration_id=integration_id,
        action_id="pull_observations",
        source_id=source_id
    )

    mock_redis.Redis.return_value.delete.assert_called_once_with(
        f"integration_state.{integration_id}.pull_observations.{source_id}"
    )


@pytest.mark.asyncio
async def test_increment_counter_calls_the_atomic_script_and_returns_its_value(mocker, mock_redis, integration_v2):
    """increment_counter is a single, unretried eval of _INCREMENT_COUNTER_SCRIPT
    (self-heal + INCR + EXPIRE all inside one Redis-side script — review
    finding: doing those steps as separate client-side calls let two callers
    racing a legacy value interleave, and could leave a freshly-incremented
    key with no TTL if the process died between steps). Not wrapped in a
    stamina retry either: a lost reply after the script actually ran would
    make a retry double-count the streak (same reasoning as every other
    non-idempotent write in this module)."""
    mocker.patch("app.services.state.redis", mock_redis)
    mock_redis.Redis.return_value.eval.return_value = async_return(3)
    state_manager = IntegrationStateManager()
    integration_id = str(integration_v2.id)

    value = await state_manager.increment_counter(
        integration_id, "pull_observations", source_id="slot_skip_streak", ttl_seconds=3600
    )

    assert value == 3
    mock_redis.Redis.return_value.eval.assert_called_once_with(
        _INCREMENT_COUNTER_SCRIPT,
        1,
        f"integration_state.{integration_id}.pull_observations.slot_skip_streak",
        3600,
    )


@pytest.mark.asyncio
async def test_increment_counter_script_is_never_retried(mocker, integration_v2):
    """A Redis error from the script call must propagate immediately, with no
    stamina retry: retrying a non-idempotent increment risks double-counting
    the streak on a lost reply after the server actually applied it (review
    finding)."""
    from redis.exceptions import RedisError

    state_manager = IntegrationStateManager()
    state_manager.db_client = mocker.MagicMock()
    state_manager.db_client.eval = mocker.AsyncMock(side_effect=RedisError("blip"))
    integration_id = str(integration_v2.id)

    with pytest.raises(RedisError):
        await state_manager.increment_counter(
            integration_id, "pull_observations", source_id="slot_skip_streak", ttl_seconds=3600
        )

    assert state_manager.db_client.eval.await_count == 1


def test_increment_counter_script_self_heals_before_expiring():
    """Script-text pin (fakeredis/a real Lua engine isn't available in this
    suite, so — in the style of the lotek_connections.py Lua tests — this
    pins the guarantee at the source level): the self-heal (DEL + re-INCR on
    a "not an integer" legacy value) must happen before the unconditional
    EXPIRE, and EXPIRE must run on every path (fresh INCR or self-healed),
    so a key is never observed as incremented with no TTL (review finding).
    An unrelated error must not trigger the self-heal — it returns an error
    reply instead, surfacing as a ResponseError to the caller untouched."""
    script = _INCREMENT_COUNTER_SCRIPT

    not_an_integer_at = script.lower().find("not an integer")
    del_at = script.find("redis.call('DEL'")
    error_reply_at = script.find("redis.error_reply")
    expire_at = script.find("redis.call('EXPIRE'")

    assert -1 not in (not_an_integer_at, del_at, error_reply_at, expire_at)
    # An unrelated error is rejected (error_reply) before the legacy value is
    # ever assumed and dropped.
    assert error_reply_at < del_at
    # The self-heal runs before EXPIRE, and EXPIRE is unconditional (outside
    # any per-branch guard) so it is reached on every path.
    assert del_at < expire_at
    assert script.rstrip().splitlines()[-2].strip().startswith("redis.call('EXPIRE'")


def test_increment_counter_script_self_heals_a_legacy_value_when_actually_run():
    """Behavioral counterpart to the text-pin above, executing the real
    script (not a hand-copied transliteration of it) via the tiny Lua-subset
    interpreter in ._lua_lite — no Lua runtime is available in this suite
    without adding a new project dependency (see the module docstring in
    _lua_lite.py for why fakeredis[lua]/lupa were ruled out here). A legacy
    JSON value must self-heal into 1, not raise, and the key must come out
    with the caller's TTL — this is exactly the guarantee the deleted
    test_increment_counter_self_heals_a_legacy_json_value covered before the
    self-heal moved server-side into this script (review finding: `redis.
    pcall` regressing to `redis.call` here would make the self-heal dead and
    the DISPATCHER_SKIP_WARN_AFTER diagnostic permanently unreachable)."""
    fake = FakeRedis()
    fake.store["k"] = '{"streak": 2}'

    value = run_script(_INCREMENT_COUNTER_SCRIPT, fake, ["k"], [3600])

    assert value == 1
    assert fake.ttls["k"] == 3600


def test_increment_counter_script_expires_with_the_given_ttl_when_actually_run():
    """The EXPIRE call must use the caller's ttl_seconds (ARGV[1]), not a
    hardcoded literal — a streak counter TTL'd out after a fixed 1 second
    could never accumulate past 1 (review finding). Executed via the real
    script, as above."""
    fake = FakeRedis()

    value = run_script(_INCREMENT_COUNTER_SCRIPT, fake, ["k"], [3600])

    assert value == 1
    assert fake.ttls["k"] == 3600


def test_increment_counter_script_rejects_an_unrelated_error_when_actually_run():
    """Only the specific "not an integer" legacy-value error self-heals; any
    other INCR failure must surface untouched (as redis.error_reply, which
    redis-py raises as a ResponseError) rather than being treated as a
    legacy value and silently dropped."""

    class _UnrelatedFailureRedis(FakeRedis):
        def _dispatch(self, cmd, args):
            if cmd.upper() == "INCR":
                raise LuaError("ERR unrelated server problem")
            return super()._dispatch(cmd, args)

    fake = _UnrelatedFailureRedis()
    fake.store["k"] = "5"

    with pytest.raises(LuaError, match="unrelated server problem"):
        run_script(_INCREMENT_COUNTER_SCRIPT, fake, ["k"], [3600])

    # The self-heal must not have fired: the original value is untouched.
    assert fake.store["k"] == "5"


@pytest.mark.asyncio
async def test_increment_counter_reraises_unrelated_response_error(mocker, integration_v2):
    """A genuine server-side ResponseError (not the specific legacy-value
    message) must surface to the caller untouched — the real Lua script does
    this via redis.error_reply, which redis-py raises as a ResponseError;
    simulated here directly against the client since the script itself only
    has coverage-by-inspection above."""
    from redis.exceptions import ResponseError

    state_manager = IntegrationStateManager()
    state_manager.db_client = mocker.MagicMock()
    state_manager.db_client.eval = mocker.AsyncMock(
        side_effect=ResponseError("ERR some unrelated server problem")
    )
    integration_id = str(integration_v2.id)

    with pytest.raises(ResponseError):
        await state_manager.increment_counter(
            integration_id, "pull_observations", source_id="slot_skip_streak", ttl_seconds=3600
        )

    assert state_manager.db_client.eval.await_count == 1



@pytest.mark.asyncio
async def test_acquire_lease_calls_the_atomic_script(mocker, mock_redis, integration_v2):
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.state.uuid.uuid4", return_value="fixed-token")
    mock_redis.Redis.return_value.eval.return_value = async_return(1)
    state_manager = IntegrationStateManager()
    integration_id = str(integration_v2.id)

    token = await state_manager.acquire_lease(
        integration_id, "backfill_observations", ttl_seconds=540, source_id="backfill_trigger_claim"
    )

    assert token == "fixed-token"
    mock_redis.Redis.return_value.eval.assert_called_once_with(
        _ACQUIRE_LEASE_SCRIPT,
        1,
        f"integration_state.{integration_id}.backfill_observations.backfill_trigger_claim",
        json.dumps("fixed-token"),
        540,
    )


@pytest.mark.asyncio
async def test_acquire_lease_returns_none_when_a_different_token_already_holds_it(mocker, mock_redis, integration_v2):
    mocker.patch("app.services.state.redis", mock_redis)
    mock_redis.Redis.return_value.eval.return_value = async_return(0)
    state_manager = IntegrationStateManager()
    integration_id = str(integration_v2.id)

    token = await state_manager.acquire_lease(
        integration_id, "backfill_observations", ttl_seconds=540, source_id="backfill_trigger_claim"
    )

    assert token is None


def test_acquire_lease_script_recognizes_its_own_token_before_the_generic_refusal():
    """Script-text pin (no real Lua engine in this suite — same style as the
    other Lua tests): a lost reply on the attempt that actually won must not
    make a stamina retry of the SAME call see its own token and report
    "already held" by someone else (review finding). The same-token branch
    must be checked, and must return success (1), before falling through to
    the generic "someone else holds it" refusal.

    (Renamed from ...before_the_ceiling_check: _ACQUIRE_LEASE_SCRIPT has no
    ceiling check — that phrase was copy-pasted from the connection-slot
    test — it only has the same-token fast path and a generic refusal.)"""
    script = _ACQUIRE_LEASE_SCRIPT

    absent_at = script.find("current == false")
    same_token_at = script.find("current == ARGV[1]")
    refusal_at = script.rstrip().splitlines()[-1].strip()

    assert -1 not in (absent_at, same_token_at)
    assert absent_at < same_token_at
    assert refusal_at == "return 0"
    # Both the fresh-acquire and same-token branches return success.
    assert script.count("return 1") == 2


def test_acquire_lease_script_sets_ttl_from_argv_when_actually_run():
    """Behavioral counterpart to the text-pin above, executing the real
    script via the tiny Lua-subset interpreter in ._lua_lite (see that
    module's docstring for why no Lua runtime dependency was added). The
    fresh-acquire branch's SET must carry an expiry from ARGV[2] — dropping
    'EX', ARGV[2] would leave the claim permanently set, permanently
    suppressing the backfill trigger for that integration (review
    finding)."""
    fake = FakeRedis()

    value = run_script(_ACQUIRE_LEASE_SCRIPT, fake, ["k"], ["tok-a", 540])

    assert value == 1
    assert fake.store["k"] == "tok-a"
    assert fake.ttls.get("k") == 540


def test_acquire_lease_script_recognizes_its_own_token_when_actually_run():
    """A retry that presents the SAME token as the key's current holder must
    succeed (and refresh the TTL from ARGV[2]) rather than falling through to
    the generic refusal — the same-token fast path this whole script exists
    to add. Executed via the real script, as above."""
    fake = FakeRedis()
    fake.store["k"] = "tok-a"
    fake.ttls["k"] = 1

    value = run_script(_ACQUIRE_LEASE_SCRIPT, fake, ["k"], ["tok-a", 540])

    assert value == 1
    assert fake.ttls["k"] == 540


def test_acquire_lease_script_refuses_a_different_token_when_actually_run():
    """A different caller's token against an already-held key must be
    refused (0), not silently granted."""
    fake = FakeRedis()
    fake.store["k"] = "tok-other"

    value = run_script(_ACQUIRE_LEASE_SCRIPT, fake, ["k"], ["tok-a", 540])

    assert value == 0


@pytest.mark.asyncio
async def test_acquire_lease_retries_present_the_identical_token_on_every_attempt(
    mocker, integration_v2, monkeypatch
):
    """The token is generated once, before the retry loop (review finding):
    moving `token = str(uuid.uuid4())` inside the loop would make a
    lost-reply retry present a FRESH token on the second attempt, so the
    script's same-token fast path no longer recognizes it as the same caller
    and falsely reports "already held" by someone else — exactly the failure
    the fast path exists to fix. A lost reply is simulated as a RedisError on
    the first eval (the real call may have succeeded server-side; the client
    just never saw the reply), then a real reply on the retry."""
    from redis.exceptions import RedisError

    _fast_stamina(monkeypatch)
    state_manager = IntegrationStateManager()
    state_manager.db_client = mocker.MagicMock()
    state_manager.db_client.eval = mocker.AsyncMock(side_effect=[RedisError("blip"), 1])
    integration_id = str(integration_v2.id)

    token = await state_manager.acquire_lease(
        integration_id, "backfill_observations", ttl_seconds=540, source_id="backfill_trigger_claim"
    )

    assert token is not None
    assert state_manager.db_client.eval.await_count == 2
    first_argv1 = state_manager.db_client.eval.await_args_list[0].args[3]
    second_argv1 = state_manager.db_client.eval.await_args_list[1].args[3]
    assert first_argv1 == second_argv1


@pytest.mark.asyncio
async def test_release_lease_is_compare_and_delete(mocker, mock_redis, integration_v2):
    mocker.patch("app.services.state.redis", mock_redis)
    mock_redis.Redis.return_value.eval.return_value = async_return(1)
    state_manager = IntegrationStateManager()
    integration_id = str(integration_v2.id)

    deleted = await state_manager.release_lease(
        integration_id, "backfill_observations", "my-token", source_id="backfill_trigger_claim"
    )

    assert deleted is True
    mock_redis.Redis.return_value.eval.assert_called_once_with(
        _RELEASE_LEASE_SCRIPT,
        1,
        f"integration_state.{integration_id}.backfill_observations.backfill_trigger_claim",
        json.dumps("my-token"),
    )


@pytest.mark.asyncio
async def test_release_lease_does_not_delete_a_successors_lease(mocker, mock_redis, integration_v2):
    """A stale releaser (its own lease already expired and re-acquired by
    someone else) must not delete the new holder's lease — the script only
    deletes when the stored token still matches the caller's."""
    mocker.patch("app.services.state.redis", mock_redis)
    mock_redis.Redis.return_value.eval.return_value = async_return(0)
    state_manager = IntegrationStateManager()
    integration_id = str(integration_v2.id)

    deleted = await state_manager.release_lease(
        integration_id, "backfill_observations", "stale-token", source_id="backfill_trigger_claim"
    )

    assert deleted is False
