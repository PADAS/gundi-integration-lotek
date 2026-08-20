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
async def test_shard_defers_starved_devices_without_retriggering(
    mocker, lotek_integration, pull_config, mock_redis
):
    # A fully saturated budget defers every starved device (spec D3: narrow
    # deferral, not the whole tail — here they happen to be the same set
    # because every device starves) and does NOT immediately re-trigger a
    # shard for them; they wait for the next scheduled tick. No device
    # failures, no ERROR-level noise, no zero-progress raise.
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
    assert shard_calls == []
    assert LogLevel.ERROR not in [c.kwargs.get("level") for c in log.await_args_list]


@pytest.mark.asyncio
async def test_starved_device_does_not_abort_the_whole_shard(
    mocker, lotek_integration, pull_config, mock_redis
):
    """One device losing the slot race must defer THAT device, not the shard's
    entire remaining tail (spec D3). Its in-chunk peers keep their results and
    the loop carries on to later chunks."""
    _setup_pull_mocks(mocker, mock_redis, [])
    mocker.patch("app.actions.handlers.get_pull_config", return_value=pull_config)

    devices = [f"dev{i}" for i in range(10)]
    calls = []

    async def fake_head_pass(device_id, *args, **kwargs):
        calls.append(device_id)
        if device_id == "dev0":
            raise NoConnectionSlot("saturated")
        return (5, False, False)

    mocker.patch("app.actions.handlers._head_pass_device", side_effect=fake_head_pass)
    mocker.patch("app.actions.handlers._retrigger_shard", return_value="handed_off")

    result = await action_pull_observations_shard(
        lotek_integration, _shard_config(*devices)
    )

    # Every device was attempted, not just the first chunk.
    assert len(calls) == 10
    # Only the starved one is deferred.
    assert result["devices_deferred"] == ["dev0"]
    # The other nine still delivered.
    assert result["observations_extracted"] == 45


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
    # zero-progress alarm fires (systemic degradation must alert) — as an ERROR
    # activity event plus a result flag, never a raise (a raise would leak the
    # integration's config through the runner's generic error handler).
    result = await action_pull_observations_shard(
        lotek_integration, _shard_config(*(str(i) for i in range(1, 9)))
    )
    assert result["zero_progress"] is True

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
async def test_slot_starvation_at_retrigger_cap_still_defers_without_error(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Slot starvation no longer attempts a re-trigger at all (spec D3: it
    # defers only the starved devices to the next scheduled tick), so the
    # generation being at the re-trigger cap must not matter to it — no
    # cap-reached ERROR, just the ordinary WARNING deferral. The cap's ERROR
    # path is still exercised via the deadline cut (see
    # test_retrigger_cap_does_not_also_emit_zero_progress).
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
    assert LogLevel.ERROR not in [c.kwargs.get("level") for c in log.await_args_list]


@pytest.mark.asyncio
async def test_slot_starvation_never_raises_and_does_not_retrigger(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Starvation is a clean back-off to the next scheduled tick — NOT systemic
    # degradation, and (spec D3) not even an attempt to re-trigger, so a dead
    # pubsub can't turn a starved shard into a raise either (review blocker,
    # both directions; the old condition raised the zero-progress
    # LotekException here when the re-trigger publish it used to attempt
    # also failed).
    _setup_pull_mocks(mocker, mock_redis, [])
    mocker.patch("app.actions.handlers.get_pull_config", return_value=pull_config)
    trigger = mocker.patch(
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
    shard_calls = [c for c in trigger.await_args_list if c.args[1] == "pull_observations_shard"]
    assert shard_calls == []


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


@pytest.mark.asyncio
async def test_dispatch_failures_do_not_abort_remaining_shards(
    mocker, lotek_integration, pull_config, mock_redis
):
    # One pubsub blip must not abort the fan-out (and must never escape into
    # the runner's config-embedding generic error handler).
    device_ids = [str(i) for i in range(1, SHARD_SIZE * 3 + 1)]  # 3 shards
    _setup_pull_mocks(mocker, mock_redis, _devices(*device_ids))
    log = mocker.patch("app.actions.handlers.log_action_activity", new=AsyncMock())
    calls = []

    async def flaky_trigger(integration_id, action_id, config=None):
        calls.append(config)
        if len(calls) == 2:
            raise Exception("pubsub blip")

    mocker.patch("app.actions.handlers.trigger_action", new=flaky_trigger)

    result = await action_pull_observations(lotek_integration, pull_config)

    assert result["shards_triggered"] == 2  # shard 2 failed, 1 and 3 dispatched
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_all_dispatches_failing_reports_without_raising(mocker, lotek_integration, pull_config, mock_redis):
    _setup_pull_mocks(mocker, mock_redis, _devices("1"))
    mocker.patch("app.actions.handlers.log_action_activity", new=AsyncMock())
    mocker.patch(
        "app.actions.handlers.trigger_action",
        new=AsyncMock(side_effect=Exception("commands topic down")),
    )
    result = await action_pull_observations(lotek_integration, pull_config)
    assert result["shards_triggered"] == 0
    assert result["devices_undispatched"] == ["1"]


@pytest.mark.asyncio
async def test_manual_run_shards_bypass_the_pause(mocker, lotek_integration, pull_config, mock_redis):
    # Portal Trigger on a paused integration: the runner bypasses the pause
    # for the manual dispatcher run, and the shards it publishes must carry
    # manual_run so they don't skip (review finding: they used to, showing a
    # successful run that pulled nothing).
    _setup_pull_mocks(mocker, mock_redis, _devices("1"))
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    pull_config.run_on_schedule = False  # paused; reaching the dispatcher means manual

    await action_pull_observations(lotek_integration, pull_config)

    shard_config = trigger.await_args_list[0].kwargs["config"]
    assert shard_config.manual_run is True

    # And the shard itself honors the marker instead of skipping.
    mocker.patch("app.actions.handlers.get_pull_config", return_value=pull_config)
    result = await action_pull_observations_shard(lotek_integration, shard_config)
    assert result.get("reason") != "integration_paused"


@pytest.mark.asyncio
async def test_only_one_shard_triggers_backfill_per_window(
    mocker, lotek_integration, pull_config, mock_redis
):
    # 25 shards of a gapped fleet used to each publish a backfill command (the
    # lease only exists a pubsub hop later). The atomic claim lets exactly one
    # win; the losers publish nothing (review finding).
    from app.services.state import IntegrationStateManager
    _setup_pull_mocks(mocker, mock_redis, [], saved_state=None)
    mocker.patch("app.actions.handlers.get_pull_config", return_value=pull_config)
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())

    claims = {"count": 0}

    async def claim_once(self, integration_id, action_id, *, ttl_seconds, source_id="no-source"):
        claims["count"] += 1
        return claims["count"] == 1  # first caller wins, rest lose

    mocker.patch.object(IntegrationStateManager, "set_if_absent", claim_once)

    for _ in range(3):  # three concurrent-ish shards of the same gapped fleet
        await action_pull_observations_shard(lotek_integration, _shard_config("1"))

    backfill_calls = [c for c in trigger.await_args_list if c.args[1] == "backfill_observations"]
    assert len(backfill_calls) == 1


@pytest.mark.asyncio
async def test_manual_run_propagates_to_the_backfill_trigger(
    mocker, lotek_integration, pull_config, mock_redis
):
    # A manual Trigger on a paused integration must import history too: without
    # the marker the backfill skipped on the pause and the run silently covered
    # only the head window (review finding).
    _setup_pull_mocks(mocker, mock_redis, [], saved_state=None)
    pull_config.run_on_schedule = False
    mocker.patch("app.actions.handlers.get_pull_config", return_value=pull_config)
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())

    config = PullObservationsShardConfig(devices=["1"], manual_run=True)
    await action_pull_observations_shard(lotek_integration, config)

    backfill_calls = [c for c in trigger.await_args_list if c.args[1] == "backfill_observations"]
    assert len(backfill_calls) == 1
    assert backfill_calls[0].kwargs["config"].manual_run is True


@pytest.mark.asyncio
async def test_retrigger_cap_does_not_also_emit_zero_progress(
    mocker, lotek_integration, pull_config, mock_redis
):
    # One load event, one signal: a cap-reached deadline cut already alerts at
    # ERROR, so the zero-progress alert must not fire on top of it.
    import itertools
    from app.actions.handlers import SHARD_RETRIGGER_CAP
    _setup_pull_mocks(mocker, mock_redis, [])
    mocker.patch("app.actions.handlers.get_pull_config", return_value=pull_config)
    mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    log = mocker.patch("app.actions.handlers.log_action_activity", new=AsyncMock())
    mocker.patch("app.actions.handlers._deadline_exceeded", return_value=True)

    config = PullObservationsShardConfig(devices=["1", "2"], generation=SHARD_RETRIGGER_CAP)
    result = await action_pull_observations_shard(lotek_integration, config)

    assert "zero_progress" not in result
    errors = [c.kwargs["title"] for c in log.await_args_list if c.kwargs.get("level") == LogLevel.ERROR]
    assert len(errors) == 1
    assert "re-trigger cap" in errors[0]


@pytest.mark.asyncio
async def test_backfill_honors_manual_run_on_a_paused_integration(
    mocker, lotek_integration, pull_config, mock_redis, auth_config
):
    from app.actions.configurations import BackfillObservationsConfig
    from app.actions.handlers import action_backfill_observations
    _setup_pull_mocks(mocker, mock_redis, [])
    pull_config.run_on_schedule = False
    mocker.patch("app.actions.handlers.get_pull_config", return_value=pull_config)
    mocker.patch("app.actions.handlers.get_auth_config", return_value=auth_config)
    mocker.patch(
        "app.services.state.IntegrationStateManager.acquire_lease",
        new=AsyncMock(return_value=None),  # stop right after the pause check
    )

    paused = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig(manual_run=False)
    )
    assert paused == {"skipped": True, "reason": "integration_paused"}

    manual = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig(manual_run=True)
    )
    assert manual["reason"] != "integration_paused"
