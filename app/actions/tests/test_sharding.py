import pytest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from gundi_core.schemas.v2 import LogLevel
from app.actions.configurations import PullObservationsShardConfig
from app.actions.handlers import (
    SHARD_SIZE,
    action_pull_observations,
    action_pull_observations_shard,
)
from app.services.lotek_connections import NoConnectionSlot
from .test_handlers import _devices, _setup_pull_mocks


# --- Parent dispatcher -------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_observations_dispatches_shards(mocker, lotek_integration, pull_config, mock_redis):
    # GUNDI-5620: the scheduled action only lists devices and fans them out as
    # pull_observations_shard sub-actions of SHARD_SIZE ids each — the whole
    # fleet never has to fit one action budget.
    device_ids = [str(i) for i in range(1, SHARD_SIZE * 2 + 6)]  # 2 full shards + 5
    _setup_pull_mocks(mocker, mock_redis, _devices(*device_ids))
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())

    result = await action_pull_observations(lotek_integration, pull_config)

    assert result == {"devices_found": len(device_ids), "shards_triggered": 3}
    shard_calls = [c for c in trigger.await_args_list if c.args[1] == "pull_observations_shard"]
    assert len(shard_calls) == 3
    dispatched = [d for c in shard_calls for d in c.kwargs["config"].devices]
    assert sorted(dispatched) == sorted(device_ids)  # every device exactly once
    assert all(len(c.kwargs["config"].devices) <= SHARD_SIZE for c in shard_calls)


@pytest.mark.asyncio
async def test_pull_observations_orders_shards_least_fresh_first(mocker, lotek_integration, pull_config, mock_redis):
    # Devices most behind lead the first shard; devices with no saved state at
    # all are most behind by definition.
    _setup_pull_mocks(mocker, mock_redis, _devices("fresh", "stale", "new"))
    states = {
        "fresh": {"version": 2, "high_water": "2026-08-18T10:00:00+00:00"},
        "stale": {"version": 2, "high_water": "2026-08-10T10:00:00+00:00"},
        "new": None,
    }

    async def get_state(integration_id, action_id, source_id="no-source"):
        return states.get(source_id)

    mocker.patch(
        "app.services.state.IntegrationStateManager.get_state",
        new=AsyncMock(side_effect=get_state),
    )
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())

    await action_pull_observations(lotek_integration, pull_config)

    shard_calls = [c for c in trigger.await_args_list if c.args[1] == "pull_observations_shard"]
    assert shard_calls[0].kwargs["config"].devices == ["new", "stale", "fresh"]


@pytest.mark.asyncio
async def test_pull_observations_skips_cleanly_when_connection_budget_exhausted(
    mocker, lotek_integration, pull_config, mock_redis
):
    # A saturated account budget on the device listing is a clean skip (the
    # next tick retries), not an action failure.
    _setup_pull_mocks(mocker, mock_redis, _devices("1"))
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())

    @asynccontextmanager
    async def starved_slot(username, **kwargs):
        raise NoConnectionSlot("no slot")
        yield

    mocker.patch("app.actions.handlers.lotek_slot", starved_slot)

    result = await action_pull_observations(lotek_integration, pull_config)

    assert result == {"skipped": True, "reason": "no_connection_slot"}
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_pull_observations_dispatches_nothing_for_empty_device_list(
    mocker, lotek_integration, pull_config, mock_redis
):
    _setup_pull_mocks(mocker, mock_redis, [])
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    result = await action_pull_observations(lotek_integration, pull_config)
    assert result == {"devices_found": 0, "shards_triggered": 0}
    trigger.assert_not_awaited()


# --- Shard action ------------------------------------------------------------


def _shard_config(*device_ids):
    return PullObservationsShardConfig(devices=list(device_ids))


@pytest.mark.asyncio
async def test_shard_defers_and_retriggers_on_slot_starvation(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Mid-shard budget exhaustion defers the starved device plus the untouched
    # tail into a re-triggered shard (the pubsub round trip is the backoff) —
    # no device failures, no ERROR-level noise, no zero-progress raise.
    _setup_pull_mocks(mocker, mock_redis, [])
    mocker.patch("app.actions.handlers.get_pull_config", return_value=pull_config)
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    log = mocker.patch("app.actions.handlers.log_action_activity", new=AsyncMock())

    @asynccontextmanager
    async def starved_slot(username, **kwargs):
        raise NoConnectionSlot("no slot")
        yield

    mocker.patch("app.actions.handlers.lotek_slot", starved_slot)

    result = await action_pull_observations_shard(
        lotek_integration, _shard_config(*(str(i) for i in range(1, 9)))
    )

    assert result["devices_failed"] == []
    assert sorted(result["devices_deferred"], key=int) == [str(i) for i in range(1, 9)]
    shard_calls = [c for c in trigger.await_args_list if c.args[1] == "pull_observations_shard"]
    assert len(shard_calls) == 1
    assert sorted(shard_calls[0].kwargs["config"].devices, key=int) == [str(i) for i in range(1, 9)]
    assert LogLevel.ERROR not in [c.kwargs.get("level") for c in log.await_args_list]


@pytest.mark.asyncio
async def test_shard_does_not_retrigger_on_hot_breaker(
    mocker, lotek_integration, pull_config, mock_redis
):
    # A hot circuit breaker means Lotek-wide degradation: re-triggering
    # immediately would defeat the pause the breaker exists to buy. The tail
    # waits for the next scheduled tick.
    import httpx
    _setup_pull_mocks(mocker, mock_redis, [])
    mocker.patch("app.actions.handlers.get_pull_config", return_value=pull_config)
    mocker.patch(
        "app.actions.client.get_positions",
        new=AsyncMock(side_effect=httpx.ReadTimeout("Lotek timed out")),
    )
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())

    # Every device fails, the breaker trips, and nothing is re-triggered: the
    # zero-progress alarm fires (systemic degradation must alert).
    from app.actions.client import LotekException
    with pytest.raises(LotekException, match="No devices could be serviced"):
        await action_pull_observations_shard(
            lotek_integration, _shard_config(*(str(i) for i in range(1, 9)))
        )

    shard_calls = [c for c in trigger.await_args_list if c.args[1] == "pull_observations_shard"]
    assert shard_calls == []


@pytest.mark.asyncio
async def test_shard_retriggers_tail_on_deadline(
    mocker, lotek_integration, pull_config, mock_redis
):
    import itertools
    _setup_pull_mocks(mocker, mock_redis, [])
    mocker.patch("app.actions.handlers.get_pull_config", return_value=pull_config)
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    mocker.patch(
        "app.actions.handlers._deadline_exceeded",
        side_effect=itertools.chain([False] * 6, itertools.repeat(True)),
    )

    result = await action_pull_observations_shard(
        lotek_integration, _shard_config(*(str(i) for i in range(1, 9)))
    )

    assert result["devices_deferred"] == ["6", "7", "8"]
    shard_calls = [c for c in trigger.await_args_list if c.args[1] == "pull_observations_shard"]
    assert len(shard_calls) == 1
    assert shard_calls[0].kwargs["config"].devices == ["6", "7", "8"]
    assert shard_calls[0].kwargs["config"].triggered_by == "pull_observations_shard"


@pytest.mark.asyncio
async def test_shard_skips_when_integration_is_paused(mocker, lotek_integration, pull_config, mock_redis):
    # Internal actions bypass the runner's skippable_pull pause check; the
    # shard must honor the operator's pause toggle itself.
    _setup_pull_mocks(mocker, mock_redis, [])
    pull_config.run_on_schedule = False
    mocker.patch("app.actions.handlers.get_pull_config", return_value=pull_config)
    get_positions = mocker.patch("app.actions.client.get_positions", new=AsyncMock())

    result = await action_pull_observations_shard(lotek_integration, _shard_config("1"))

    assert result == {"skipped": True, "reason": "integration_paused"}
    get_positions.assert_not_awaited()


@pytest.mark.asyncio
async def test_shard_skips_quietly_when_pull_config_is_missing(mocker, lotek_integration, mock_redis):
    # Machine-triggered: raising would route the full integration config
    # (auth included) through the generic error handler. Skip quietly; the
    # scheduled parent surfaces the missing config.
    from app.services.errors import ConfigurationNotFound
    _setup_pull_mocks(mocker, mock_redis, [])
    mocker.patch(
        "app.actions.handlers.get_pull_config",
        side_effect=ConfigurationNotFound("pull config missing"),
    )

    result = await action_pull_observations_shard(lotek_integration, _shard_config("1"))

    assert result == {"skipped": True, "reason": "configuration_missing"}


@pytest.mark.asyncio
async def test_shard_config_requires_at_least_one_device():
    # An empty devices list would publish an empty config_overrides, which the
    # runner reads as "no config at all" (404 before the handler runs).
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        PullObservationsShardConfig(devices=[])


# --- Re-trigger governor + starvation alarm fixes (PR #20 review blockers) ---


@pytest.mark.asyncio
async def test_retrigger_increments_generation(mocker, lotek_integration, pull_config, mock_redis):
    import itertools
    _setup_pull_mocks(mocker, mock_redis, [])
    mocker.patch("app.actions.handlers.get_pull_config", return_value=pull_config)
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    mocker.patch(
        "app.actions.handlers._deadline_exceeded",
        side_effect=itertools.chain([False] * 6, itertools.repeat(True)),
    )

    config = PullObservationsShardConfig(devices=[str(i) for i in range(1, 9)], generation=1)
    await action_pull_observations_shard(lotek_integration, config)

    shard_calls = [c for c in trigger.await_args_list if c.args[1] == "pull_observations_shard"]
    assert shard_calls[0].kwargs["config"].generation == 2


@pytest.mark.asyncio
async def test_retrigger_cap_falls_back_to_next_tick_with_error(
    mocker, lotek_integration, pull_config, mock_redis
):
    # At the cap the tail is NOT re-triggered (next scheduled tick picks it
    # up) and the exhaustion is surfaced at ERROR — an ungoverned chain under
    # sustained starvation was an invisible busy-loop (review blocker).
    from app.actions.handlers import SHARD_RETRIGGER_CAP
    _setup_pull_mocks(mocker, mock_redis, [])
    mocker.patch("app.actions.handlers.get_pull_config", return_value=pull_config)
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    log = mocker.patch("app.actions.handlers.log_action_activity", new=AsyncMock())

    @asynccontextmanager
    async def starved_slot(username, **kwargs):
        raise NoConnectionSlot("no slot")
        yield

    mocker.patch("app.actions.handlers.lotek_slot", starved_slot)

    config = PullObservationsShardConfig(devices=["1", "2"], generation=SHARD_RETRIGGER_CAP)
    result = await action_pull_observations_shard(lotek_integration, config)  # must not raise

    assert result["devices_deferred"] == ["1", "2"]
    shard_calls = [c for c in trigger.await_args_list if c.args[1] == "pull_observations_shard"]
    assert shard_calls == []
    cap_errors = [
        c for c in log.await_args_list
        if c.kwargs.get("level") == LogLevel.ERROR and "re-trigger cap" in c.kwargs.get("title", "")
    ]
    assert len(cap_errors) == 1


@pytest.mark.asyncio
async def test_slot_starvation_never_raises_even_when_retrigger_fails(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Starvation + a failed re-trigger publish is a clean back-off plus a
    # pubsub blip — NOT systemic degradation. The old condition raised the
    # zero-progress LotekException here (review blocker, both directions).
    _setup_pull_mocks(mocker, mock_redis, [])
    mocker.patch("app.actions.handlers.get_pull_config", return_value=pull_config)
    mocker.patch(
        "app.actions.handlers.trigger_action",
        new=AsyncMock(side_effect=Exception("pubsub down")),
    )

    @asynccontextmanager
    async def starved_slot(username, **kwargs):
        raise NoConnectionSlot("no slot")
        yield

    mocker.patch("app.actions.handlers.lotek_slot", starved_slot)

    result = await action_pull_observations_shard(
        lotek_integration, _shard_config("1", "2")
    )  # must not raise

    assert sorted(result["devices_deferred"], key=int) == ["1", "2"]
    assert result["devices_failed"] == []


@pytest.mark.asyncio
async def test_dispatcher_skip_streak_warns_after_consecutive_skips(
    mocker, lotek_integration, pull_config, mock_redis
):
    # One skipped tick stays out of the portal; a streak must not stay
    # invisible (review blocker: the skip was logger.info only).
    from app.actions.handlers import DISPATCHER_SKIP_WARN_AFTER
    _setup_pull_mocks(mocker, mock_redis, _devices("1"))
    log = mocker.patch("app.actions.handlers.log_action_activity", new=AsyncMock())
    mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())

    streak_state = {}

    async def get_state(integration_id, action_id, source_id="no-source"):
        return streak_state.get(source_id)

    async def set_state(integration_id, action_id, state, source_id="no-source", **kwargs):
        streak_state[source_id] = state

    async def delete_state(integration_id, action_id, source_id="no-source"):
        streak_state.pop(source_id, None)

    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(side_effect=get_state))
    mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(side_effect=set_state))
    mocker.patch("app.services.state.IntegrationStateManager.delete_state", new=AsyncMock(side_effect=delete_state))

    @asynccontextmanager
    async def starved_slot(username, **kwargs):
        raise NoConnectionSlot("no slot")
        yield

    mocker.patch("app.actions.handlers.lotek_slot", starved_slot)

    warnings = lambda: [
        c for c in log.await_args_list
        if c.kwargs.get("level") == LogLevel.WARNING and "consecutive ticks" in c.kwargs.get("title", "")
    ]
    for tick in range(1, DISPATCHER_SKIP_WARN_AFTER + 1):
        result = await action_pull_observations(lotek_integration, pull_config)
        assert result == {"skipped": True, "reason": "no_connection_slot"}
        assert len(warnings()) == (1 if tick >= DISPATCHER_SKIP_WARN_AFTER else 0)

    # A successful tick resets the streak.
    mocker.patch("app.actions.handlers.lotek_slot", None)  # restore not needed; grant below

    @asynccontextmanager
    async def granted_slot(username, **kwargs):
        yield

    mocker.patch("app.actions.handlers.lotek_slot", granted_slot)
    await action_pull_observations(lotek_integration, pull_config)
    from app.actions.handlers import DISPATCHER_SKIP_STREAK_SOURCE
    assert DISPATCHER_SKIP_STREAK_SOURCE not in streak_state
