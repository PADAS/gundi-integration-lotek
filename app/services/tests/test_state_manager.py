import datetime
import json

import pytest
from app.conftest import async_return
from app.services.state import (
    IntegrationStateManager,
    _ACQUIRE_LEASE_SCRIPT,
    _INCREMENT_COUNTER_SCRIPT,
    _MERGE_STATE_SCRIPT,
    _RELEASE_LEASE_SCRIPT,
)


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


def test_acquire_lease_script_recognizes_its_own_token_before_the_ceiling_check():
    """Script-text pin (no real Lua engine in this suite — same style as the
    other Lua tests): a lost reply on the attempt that actually won must not
    make a stamina retry of the SAME call see its own token and report
    "already held" by someone else (review finding). The same-token branch
    must be checked, and must return success (1), before falling through to
    the generic "someone else holds it" refusal."""
    script = _ACQUIRE_LEASE_SCRIPT

    absent_at = script.find("current == false")
    same_token_at = script.find("current == ARGV[1]")
    refusal_at = script.rstrip().splitlines()[-1].strip()

    assert -1 not in (absent_at, same_token_at)
    assert absent_at < same_token_at
    assert refusal_at == "return 0"
    # Both the fresh-acquire and same-token branches return success.
    assert script.count("return 1") == 2


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
