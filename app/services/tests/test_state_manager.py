import datetime
import json

import pytest
from app.conftest import async_return
from app.services.state import IntegrationStateManager, _MERGE_STATE_SCRIPT


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
async def test_increment_counter_is_atomic_and_expires(mocker, mock_redis, integration_v2):
    """Client-side get/int+1/set loses increments under concurrency — the same
    reason merge_state_fields exists. INCR is atomic; EXPIRE stops abandoned
    counters leaking keys."""
    mocker.patch("app.services.state.redis", mock_redis)
    mock_redis.Redis.return_value.incr.return_value = async_return(3)
    mock_redis.Redis.return_value.expire.return_value = async_return(True)
    state_manager = IntegrationStateManager()
    integration_id = str(integration_v2.id)

    value = await state_manager.increment_counter(
        integration_id, "pull_observations", source_id="slot_skip_streak", ttl_seconds=3600
    )

    assert value == 3
    mock_redis.Redis.return_value.incr.assert_called_once_with(
        f"integration_state.{integration_id}.pull_observations.slot_skip_streak"
    )
    mock_redis.Redis.return_value.expire.assert_called_once_with(
        f"integration_state.{integration_id}.pull_observations.slot_skip_streak", 3600
    )


@pytest.mark.asyncio
async def test_increment_counter_expire_retry_does_not_reincrement(mocker, integration_v2, monkeypatch):
    """EXPIRE keeps its own retry loop, isolated from INCR (which is not
    retried at all — see test_increment_counter_incr_is_never_retried):
    retrying INCR+EXPIRE as one block means a RedisError on EXPIRE re-runs
    INCR too, silently over-counting the streak (review finding M3)."""
    import stamina
    from redis.exceptions import RedisError

    # Keep the (real) retry loop from sleeping between attempts; the `redis`
    # name in app.services.state must stay the real module here (not a mock)
    # so `on=redis.RedisError` in the retried block still matches a real
    # RedisError instance.
    real_retry_context = stamina.retry_context

    def fast_retry_context(*args, **kwargs):
        kwargs["wait_initial"] = 0
        kwargs["wait_max"] = 0
        kwargs["wait_jitter"] = 0
        return real_retry_context(*args, **kwargs)

    monkeypatch.setattr(stamina, "retry_context", fast_retry_context)

    state_manager = IntegrationStateManager()
    state_manager.db_client = mocker.MagicMock()
    state_manager.db_client.incr = mocker.AsyncMock(return_value=5)
    state_manager.db_client.expire = mocker.AsyncMock(
        side_effect=[RedisError("blip"), True]
    )
    integration_id = str(integration_v2.id)

    value = await state_manager.increment_counter(
        integration_id, "pull_observations", source_id="slot_skip_streak", ttl_seconds=3600
    )

    assert value == 5
    # EXPIRE's own retry must not re-run INCR.
    assert state_manager.db_client.incr.await_count == 1
    assert state_manager.db_client.expire.await_count == 2


@pytest.mark.asyncio
async def test_increment_counter_incr_is_never_retried(mocker, integration_v2):
    """INCR is a non-idempotent write: retrying it risks double-counting a
    streak on a lost reply (the server applied it, the client never saw the
    reply), and if every retry attempt fails that way EXPIRE is never reached,
    leaving an inflated key with no TTL (review finding). A single unretried
    INCR call accepts an occasional missed increment instead — the caller
    already treats any failure as a safe default."""
    from redis.exceptions import RedisError

    state_manager = IntegrationStateManager()
    state_manager.db_client = mocker.MagicMock()
    state_manager.db_client.incr = mocker.AsyncMock(side_effect=RedisError("blip"))
    state_manager.db_client.expire = mocker.AsyncMock(return_value=True)
    integration_id = str(integration_v2.id)

    with pytest.raises(RedisError):
        await state_manager.increment_counter(
            integration_id, "pull_observations", source_id="slot_skip_streak", ttl_seconds=3600
        )

    assert state_manager.db_client.incr.await_count == 1
    state_manager.db_client.expire.assert_not_awaited()

@pytest.mark.asyncio
async def test_increment_counter_self_heals_a_legacy_json_value(mocker, integration_v2):
    """Before this counter existed, _bump_dispatcher_skip_streak stored the
    same key as a JSON blob via set_state (e.g. {"streak": 2}), with no TTL —
    so that key is permanent, and post-deploy INCR against it always raises
    "value is not an integer or out of range" (review finding: this made
    DISPATCHER_SKIP_WARN_AFTER permanently unreachable for any integration
    carrying one). The first INCR to hit a legacy value must self-heal: drop
    the stale value and increment once more so the call still returns 1."""
    from redis.exceptions import ResponseError

    state_manager = IntegrationStateManager()
    state_manager.db_client = mocker.MagicMock()
    state_manager.db_client.incr = mocker.AsyncMock(
        side_effect=[ResponseError("value is not an integer or out of range"), 1]
    )
    state_manager.db_client.delete = mocker.AsyncMock(return_value=1)
    state_manager.db_client.expire = mocker.AsyncMock(return_value=True)
    integration_id = str(integration_v2.id)
    key = f"integration_state.{integration_id}.pull_observations.slot_skip_streak"

    value = await state_manager.increment_counter(
        integration_id, "pull_observations", source_id="slot_skip_streak", ttl_seconds=3600
    )

    assert value == 1
    state_manager.db_client.delete.assert_awaited_once_with(key)
    assert state_manager.db_client.incr.await_count == 2
    state_manager.db_client.expire.assert_awaited_once_with(key, 3600)


@pytest.mark.asyncio
async def test_increment_counter_reraises_unrelated_response_error(mocker, integration_v2, monkeypatch):
    """Catching ResponseError must not swallow a genuine server-side problem
    that happens to share the exception class — only the specific
    not-an-integer message identifies a legacy value; anything else must
    surface untouched rather than be silently treated as a migration."""
    import stamina
    from redis.exceptions import ResponseError

    # INCR is not retried at all (see test_increment_counter_incr_is_never_
    # retried), so this no longer needs the retry loop zeroed to run fast —
    # kept anyway in case EXPIRE's own loop is ever reached on this path.
    real_retry_context = stamina.retry_context

    def fast_retry_context(*args, **kwargs):
        kwargs["wait_initial"] = 0
        kwargs["wait_max"] = 0
        kwargs["wait_jitter"] = 0
        return real_retry_context(*args, **kwargs)

    monkeypatch.setattr(stamina, "retry_context", fast_retry_context)

    state_manager = IntegrationStateManager()
    state_manager.db_client = mocker.MagicMock()
    state_manager.db_client.incr = mocker.AsyncMock(
        side_effect=ResponseError("ERR some unrelated server problem")
    )
    state_manager.db_client.delete = mocker.AsyncMock()
    state_manager.db_client.expire = mocker.AsyncMock(return_value=True)
    integration_id = str(integration_v2.id)

    with pytest.raises(ResponseError):
        await state_manager.increment_counter(
            integration_id, "pull_observations", source_id="slot_skip_streak", ttl_seconds=3600
        )

    state_manager.db_client.delete.assert_not_awaited()
    state_manager.db_client.expire.assert_not_awaited()
