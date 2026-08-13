import pytest
import httpx

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from gundi_core.schemas.v2 import LogLevel
from app.actions.handlers import (
    BACKFILL_MAX_WINDOWS_PER_DEVICE,
    RETRY_ATTEMPTS,
    action_backfill_observations,
)
from app.actions.client import LotekDevice, LotekException
from app.actions.configurations import BackfillObservationsConfig, PullObservationsConfig


def _devices(*device_ids):
    return [
        LotekDevice(nDeviceID=d, strSpecialID="s", dtCreated=datetime.now(), strSatellite="sat")
        for d in device_ids
    ]


def _gap_state(days_back_start=7, days_back_end=1, last_backfilled=None):
    now = datetime.now(timezone.utc)
    state = {
        "high_water": now.isoformat(),
        "gap_start": (now - timedelta(days=days_back_start)).isoformat(),
        "gap_end": (now - timedelta(days=days_back_end)).isoformat(),
    }
    if last_backfilled:
        state["last_backfilled"] = last_backfilled.isoformat()
    return state


def _setup_backfill_mocks(mocker, mock_redis, devices, states_by_device):
    # The lotek_integration fixture only carries an auth config; backfill also
    # reads the pull config for max_pdop — patch the config getter instead.
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=devices))
    mocker.patch("app.actions.handlers.get_pull_config", return_value=PullObservationsConfig())
    get_positions = mocker.patch(
        "app.actions.client.get_positions", new=AsyncMock(return_value=[])
    )
    mocker.patch(
        "app.services.state.IntegrationStateManager.get_state",
        new=AsyncMock(side_effect=lambda i, a, d: states_by_device.get(d, {})),
    )
    set_state = mocker.patch(
        "app.services.state.IntegrationStateManager.set_state", new=AsyncMock()
    )
    lease = mocker.patch(
        "app.services.state.IntegrationStateManager.set_if_absent",
        new=AsyncMock(return_value=True),
    )
    release = mocker.patch(
        "app.services.state.IntegrationStateManager.delete_state", new=AsyncMock()
    )
    log = mocker.patch("app.actions.handlers.log_action_activity", new=AsyncMock())
    return get_positions, set_state, lease, release, log


@pytest.mark.asyncio
async def test_backfill_skips_whole_run_when_lease_is_held(
    mocker, lotek_integration, mock_redis
):
    get_positions, _, lease, release, _ = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1"), {"1": _gap_state()}
    )
    lease.return_value = False
    result = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig()
    )
    assert result == {"skipped": "lease_held"}
    get_positions.assert_not_awaited()
    release.assert_not_awaited()  # a lease we didn't take is not ours to release


@pytest.mark.asyncio
async def test_backfill_releases_lease_even_when_the_run_raises(
    mocker, lotek_integration, mock_redis
):
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    _, _, _, release, _ = _setup_backfill_mocks(mocker, mock_redis, [], {})
    mocker.patch(
        "app.actions.client.get_devices", new=AsyncMock(side_effect=httpx.ReadTimeout(""))
    )
    with pytest.raises(httpx.ReadTimeout):
        await action_backfill_observations(lotek_integration, BackfillObservationsConfig())
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_backfill_caps_windows_per_device_and_advances_gap_start(
    mocker, lotek_integration, mock_redis
):
    # A 20-day gap needs 3 seven-day windows; only 2 (the cap) may run,
    # oldest-first, and gap_start must advance to the end of the last
    # delivered window.
    get_positions, set_state, _, _, _ = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1"), {"1": _gap_state(days_back_start=21)}
    )
    await action_backfill_observations(lotek_integration, BackfillObservationsConfig())
    assert get_positions.await_count == BACKFILL_MAX_WINDOWS_PER_DEVICE
    first_start = get_positions.await_args_list[0].args[3]
    second_start = get_positions.await_args_list[1].args[3]
    assert second_start - first_start == timedelta(days=7)
    final_state = set_state.await_args_list[-1].args[2]
    assert final_state["gap_start"] - first_start == timedelta(days=14)
    assert final_state["gap_end"] is not None  # 20d gap: still open after 2 windows


@pytest.mark.asyncio
async def test_backfill_closes_gap_to_null_when_fully_covered(
    mocker, lotek_integration, mock_redis
):
    get_positions, set_state, _, _, _ = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1"), {"1": _gap_state(days_back_start=5)}
    )
    result = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig()
    )
    assert get_positions.await_count == 1  # 4-day gap fits one 7-day window
    final_state = set_state.await_args_list[-1].args[2]
    assert final_state["gap_start"] is None and final_state["gap_end"] is None
    assert final_state["last_backfilled"] is not None
    assert result["gaps_closed"] == 1


@pytest.mark.asyncio
async def test_backfill_orders_devices_least_recently_backfilled_first(
    mocker, lotek_integration, mock_redis
):
    now = datetime.now(timezone.utc)
    states = {
        "recent": _gap_state(last_backfilled=now - timedelta(minutes=5)),
        "never": _gap_state(),  # no last_backfilled → leads
        "old": _gap_state(last_backfilled=now - timedelta(days=2)),
    }
    get_positions, _, _, _, _ = _setup_backfill_mocks(
        mocker, mock_redis, _devices("recent", "never", "old"), states
    )
    await action_backfill_observations(lotek_integration, BackfillObservationsConfig())
    order = [c.args[0] for c in get_positions.await_args_list]
    assert order == ["never", "never", "old", "old", "recent", "recent"] or order == [
        "never", "old", "recent"
    ]
    # each device has a 6-day gap → a single window; exact per-device counts
    # are covered elsewhere, ordering is what matters here
    deduped = []
    for d in order:
        if not deduped or deduped[-1] != d:
            deduped.append(d)
    assert deduped == ["never", "old", "recent"]


@pytest.mark.asyncio
async def test_backfill_does_not_advance_gap_when_send_fails(
    mocker, lotek_integration, lotek_position, mock_redis
):
    get_positions, set_state, _, _, log = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1", "2"), {"1": _gap_state(), "2": _gap_state()}
    )
    get_positions.return_value = [lotek_position]
    mocker.patch(
        "app.actions.handlers.gundi_tools.send_observations_to_gundi",
        new=AsyncMock(side_effect=[Exception("boom"), None]),
    )
    result = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig()
    )
    assert result["devices_failed"] == ["1"]
    # device 1's gap must remain open (only its last_backfilled save may run)
    device_1_gap_saves = [
        c for c in set_state.await_args_list
        if c.args[3] == "1" and c.args[2].get("gap_start") is None
    ]
    assert not device_1_gap_saves, "a failed window must not close or advance the gap"
    error_logs = [c for c in log.await_args_list if c.kwargs["level"] == LogLevel.ERROR]
    assert error_logs, "delivery failures must stay ERROR in backfill too"


@pytest.mark.asyncio
async def test_backfill_skips_devices_without_gaps(
    mocker, lotek_integration, mock_redis
):
    now = datetime.now(timezone.utc)
    states = {"1": {"high_water": now.isoformat()}, "2": _gap_state()}
    get_positions, _, _, _, _ = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1", "2"), states
    )
    await action_backfill_observations(lotek_integration, BackfillObservationsConfig())
    assert set(c.args[0] for c in get_positions.await_args_list) == {"2"}


@pytest.mark.asyncio
async def test_backfill_retriggers_itself_when_gaps_remain(
    mocker, lotek_integration, mock_redis
):
    # Movebank-pattern cascade: a run that leaves gaps open (window cap or
    # deferral) re-triggers itself so the import drains continuously instead of
    # waiting for the next head-pass tick. The re-trigger must fire AFTER the
    # lease release, or the next run would see its own lease and skip.
    calls = []
    get_positions, _, _, release, _ = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1"), {"1": _gap_state(days_back_start=21)}
    )
    release.side_effect = lambda *a, **k: calls.append("release")
    trigger = mocker.patch(
        "app.actions.handlers.trigger_action",
        new=AsyncMock(side_effect=lambda *a, **k: calls.append("trigger")),
    )
    await action_backfill_observations(lotek_integration, BackfillObservationsConfig())
    trigger.assert_awaited_once_with(str(lotek_integration.id), "backfill_observations")
    assert calls == ["release", "trigger"], "re-trigger must come after the lease release"


@pytest.mark.asyncio
async def test_backfill_does_not_retrigger_when_all_gaps_closed(
    mocker, lotek_integration, mock_redis
):
    _setup_backfill_mocks(
        mocker, mock_redis, _devices("1"), {"1": _gap_state(days_back_start=5)}
    )
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    result = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig()
    )
    assert result["gaps_closed"] == 1
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_does_not_retrigger_on_zero_progress(
    mocker, lotek_integration, mock_redis
):
    # Zero progress raises — the raise is the cascade's natural chain-breaker,
    # otherwise a wholly-failing backfill would re-trigger itself forever.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    get_positions, _, _, _, _ = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1"), {"1": _gap_state()}
    )
    get_positions.side_effect = httpx.ReadTimeout("")
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    with pytest.raises(LotekException):
        await action_backfill_observations(lotek_integration, BackfillObservationsConfig())
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_zero_progress_raises(
    mocker, lotek_integration, mock_redis
):
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    get_positions, _, _, release, _ = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1"), {"1": _gap_state()}
    )
    get_positions.side_effect = httpx.ReadTimeout("")
    with pytest.raises(LotekException, match="No devices could be backfilled"):
        await action_backfill_observations(lotek_integration, BackfillObservationsConfig())
    release.assert_awaited_once()
