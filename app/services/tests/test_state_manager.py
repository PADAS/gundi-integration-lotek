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
