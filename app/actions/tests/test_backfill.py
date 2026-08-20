import pytest
import httpx

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from gundi_core.schemas.v2 import LogLevel
from app.actions.handlers import (
    BACKFILL_MAX_WINDOWS_PER_DEVICE,
    action_backfill_observations,
)
from app.actions.client import LotekDevice
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
        "app.services.state.IntegrationStateManager.acquire_lease",
        new=AsyncMock(return_value="lease-token"),
    )
    release = mocker.patch(
        "app.services.state.IntegrationStateManager.release_lease", new=AsyncMock(return_value=True)
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
    lease.return_value = None  # acquire_lease: None = already held
    result = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig()
    )
    assert result == {"skipped": True, "reason": "lease_held"}
    get_positions.assert_not_awaited()
    release.assert_not_awaited()  # a lease we didn't take is not ours to release


@pytest.mark.asyncio
async def test_backfill_skips_when_integration_is_paused(
    mocker, lotek_integration, mock_redis
):
    # Review finding: run_on_schedule=False (the operator's pause toggle) must
    # also stop the cascade — internal actions bypass the runner's pause check,
    # so the backfill has to honor it itself.
    from app.actions.configurations import PullObservationsConfig
    get_positions, _, lease, _, _ = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1"), {"1": _gap_state()}
    )
    mocker.patch(
        "app.actions.handlers.get_pull_config",
        return_value=PullObservationsConfig(run_on_schedule=False),
    )
    result = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig()
    )
    assert result == {"skipped": True, "reason": "integration_paused"}
    lease.assert_not_awaited()
    get_positions.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_does_not_retrigger_while_breaker_is_hot(
    mocker, lotek_integration, mock_redis
):
    # Review finding: an immediate self-retrigger after a breaker trip defeats
    # the pause the breaker exists to buy — the next scheduled head pass is the
    # one that should re-trigger, ~cadence later.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    device_ids = ("1", "2", "3", "4", "5", "6", "7", "8")
    states = {d: _gap_state(days_back_start=5) for d in device_ids}
    get_positions, _, _, _, _ = _setup_backfill_mocks(
        mocker, mock_redis, _devices(*device_ids), states
    )
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())

    # Device 1 succeeds (its 4-day gap closes in one window); 2-5 exhaust
    # retries on timeouts, so the breaker is hot (streak 4 >= 3) at the chunk
    # boundary and devices 6-8 (chunk 2) are deferred with their gaps open.
    # Per-device behavior: chunked-concurrent fetching makes the call order
    # nondeterministic, so an ordered side_effect list would misfire.
    async def get_positions_side_effect(device_id, *args, **kwargs):
        if device_id == "1":
            return []
        raise httpx.ReadTimeout("")

    get_positions.side_effect = get_positions_side_effect
    result = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig()
    )
    assert result["devices_deferred"] == ["6", "7", "8"]
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_save_does_not_clobber_concurrently_advanced_high_water(
    mocker, lotek_integration, mock_redis
):
    # Review finding (lost-update race): the backfill must persist only the
    # fields it owns (gap_*, last_backfilled). A head pass advancing high_water
    # mid-backfill must survive the backfill's writes.
    now = datetime.now(timezone.utc)
    old_hw = (now - timedelta(hours=6)).isoformat()
    new_hw = (now - timedelta(minutes=1)).isoformat()
    snapshot = {**_gap_state(days_back_start=5), "high_water": old_hw}
    advanced = {**_gap_state(days_back_start=5), "high_water": new_hw}
    get_positions, set_state, _, _, _ = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1"), {}
    )
    reads = mocker.patch(
        "app.services.state.IntegrationStateManager.get_state",
        # scanner read (old hw), then merge re-reads (head pass advanced hw)
        new=AsyncMock(side_effect=[snapshot, advanced, advanced]),
    )
    await action_backfill_observations(lotek_integration, BackfillObservationsConfig())
    for c in set_state.await_args_list:
        assert str(c.args[2].get("high_water")) == new_hw, (
            "the backfill rewound a high_water a concurrent head pass had advanced"
        )


@pytest.mark.asyncio
async def test_backfill_releases_lease_even_when_the_run_raises(
    mocker, lotek_integration, mock_redis
):
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    _, _, _, release, _ = _setup_backfill_mocks(mocker, mock_redis, [], {})
    # A non-transport failure: transport errors return cleanly (WARNING) since
    # the 2026-08-16 congestion fix, so they no longer exercise the raise path.
    mocker.patch(
        "app.actions.client.get_devices", new=AsyncMock(side_effect=ValueError("boom"))
    )
    with pytest.raises(ValueError):
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
    # [-1] is the last_backfilled merge-save; [-2] is the last window's gap save
    window_save = set_state.await_args_list[-2].args[2]
    assert window_save["gap_start"] - first_start == timedelta(days=14)
    assert window_save["gap_end"] is not None  # 20d gap: still open after 2 windows
    assert set_state.await_args_list[-1].args[2]["last_backfilled"] is not None


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
    # [-1] is the last_backfilled merge-save; [-2] is the gap-close save
    window_save = set_state.await_args_list[-2].args[2]
    assert window_save["gap_start"] is None and window_save["gap_end"] is None
    assert set_state.await_args_list[-1].args[2]["last_backfilled"] is not None
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
    # Each device's 6-day gap fits a single 7-day window, so exactly one fetch
    # per device, least-recently-backfilled first (never-backfilled leads).
    order = [c.args[0] for c in get_positions.await_args_list]
    assert order == ["never", "old", "recent"]


@pytest.mark.asyncio
async def test_backfill_malformed_data_failure_logs_error_not_warning(
    mocker, lotek_integration, mock_redis
):
    # Review finding: a data-shape break is permanent, unlike a transient
    # timeout, and must stay ERROR so it can alert.
    get_positions, _, _, _, log = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1"), {"1": _gap_state()}
    )
    get_positions.return_value = None  # blows up in filter_and_transform_positions
    # Single device, so this is also a zero-progress run — reported on the
    # result now instead of raised (spec D7).
    result = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig()
    )
    assert result["zero_progress"] is True
    device_logs = [c for c in log.await_args_list if "Device: 1" in c.kwargs.get("title", "")]
    assert device_logs and all(c.kwargs["level"] == LogLevel.ERROR for c in device_logs)


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
    trigger.assert_awaited_once()
    args, kwargs = trigger.await_args
    assert args[:2] == (str(lotek_integration.id), "backfill_observations")
    config = kwargs.get("config") or args[2]
    assert config.dict() != {}, "an empty override 404s in execute_action before the handler runs"
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
    # Zero progress breaks the cascade: the raise used to be its natural
    # chain-breaker, and `not zero_progress` on the gaps_remaining gate now
    # carries that explicitly (spec D7). A wholly-failing backfill must not
    # re-trigger itself forever.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    get_positions, _, _, _, _ = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1"), {"1": _gap_state()}
    )
    get_positions.side_effect = httpx.ReadTimeout("")
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    result = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig()
    )
    assert result["zero_progress"] is True
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_zero_progress_reports_and_still_releases_the_lease(
    mocker, lotek_integration, mock_redis
):
    # Was test_backfill_zero_progress_raises: the ERROR activity event replaced
    # the raise (spec D7), so the lease release now happens on a normal return
    # through the finally rather than while an exception unwinds.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    get_positions, _, _, release, log = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1"), {"1": _gap_state()}
    )
    get_positions.side_effect = httpx.ReadTimeout("")
    result = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig()
    )
    assert result["zero_progress"] is True
    zero_progress_errors = [
        c for c in log.await_args_list
        if c.kwargs.get("level") == LogLevel.ERROR
        and "No devices could be backfilled" in c.kwargs.get("title", "")
    ]
    assert zero_progress_errors, "the health signal must still reach the activity feed"
    release.assert_awaited_once()


# --- chrisdoehring review fix round ------------------------------------------


@pytest.mark.asyncio
async def test_backfill_does_not_retrigger_when_no_window_advanced(
    mocker, lotek_integration, lotek_position, mock_redis
):
    # Review finding: with a deterministic partial-delivery failure (batch 1
    # lands, a later batch is always rejected), the run counts extracted
    # observations — dodging the zero-progress raise — but never advances the
    # gap, so retriggering on has_gap alone spun an unthrottled tight loop.
    # No window advanced → no cascade; the next head pass retries ~cadence later.
    from app.settings.integration import OBSERVATIONS_BATCH_SIZE
    get_positions, _, _, _, _ = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1"), {"1": _gap_state()}
    )
    get_positions.return_value = [lotek_position] * (OBSERVATIONS_BATCH_SIZE + 1)
    mocker.patch(
        "app.actions.handlers.gundi_tools.send_observations_to_gundi",
        new=AsyncMock(side_effect=[None, Exception("422 rejected")]),
    )
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    result = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig()
    )
    assert result["observations_extracted"] == OBSERVATIONS_BATCH_SIZE  # batch 1 landed
    assert result["devices_failed"] == ["1"]
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_still_retriggers_while_windows_are_advancing(
    mocker, lotek_integration, mock_redis
):
    # Companion to the no-progress gate: a run that IS draining (window cap
    # hit, gap still open) keeps the cascade going.
    _setup_backfill_mocks(
        mocker, mock_redis, _devices("1"), {"1": _gap_state(days_back_start=30)}
    )
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    await action_backfill_observations(lotek_integration, BackfillObservationsConfig())
    trigger.assert_awaited_once()


@pytest.mark.asyncio
async def test_backfill_skips_quietly_when_config_is_missing(
    mocker, lotek_integration, mock_redis
):
    # Review finding: an operator unconfiguring pull/auth mid-cascade made the
    # backfill raise into the runner's generic _handle_error — an ERROR event
    # embedding the full config (auth included) on every remaining cascade
    # step. Missing config now skips; the head pass surfaces it.
    from app.services.errors import ConfigurationNotFound
    get_positions, _, lease, _, log = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1"), {"1": _gap_state()}
    )
    mocker.patch(
        "app.actions.handlers.get_pull_config",
        side_effect=ConfigurationNotFound("pull config missing"),
    )
    result = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig()
    )
    assert result == {"skipped": True, "reason": "configuration_missing"}
    lease.assert_not_awaited()
    get_positions.assert_not_awaited()
    error_logs = [c for c in log.await_args_list if c.kwargs.get("level") == LogLevel.ERROR]
    assert not error_logs


# --- spec D7: zero progress reports instead of raising -----------------------


@pytest.mark.asyncio
async def test_zero_progress_backfill_reports_instead_of_raising(
    mocker, lotek_integration, mock_redis
):
    """Raising routed through the runner's generic _handle_error, which
    publishes config_data containing the integration's plaintext auth
    (GUNDI-5628). Backfill adopts the head pass's ERROR-event + result-flag
    contract instead (spec D7), and must still suppress the self-retrigger."""
    _setup_backfill_mocks(mocker, mock_redis, _devices("1"), {"1": _gap_state()})
    mocker.patch(
        "app.actions.handlers._backfill_device",
        new=AsyncMock(side_effect=ValueError("boom")),
    )
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    try_log = mocker.patch("app.actions.handlers._try_log_activity", new=AsyncMock())

    result = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig(triggered_by="test")
    )

    assert result["zero_progress"] is True
    # ERROR activity event carries the health signal...
    assert try_log.await_args.args[3] is LogLevel.ERROR
    # ...and the cascade stays broken, exactly as the raise used to guarantee.
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_zero_progress_backfill_breaks_the_cascade_after_a_window_advanced(
    mocker, lotek_integration, mock_redis
):
    # The removed raise broke the cascade implicitly by unwinding before the
    # re-trigger. A run that advanced an empty window and then failed satisfies
    # every other gaps_remaining conjunct, so `not zero_progress` has to be
    # explicit or a wholly-failing backfill would re-trigger itself forever.
    get_positions, _, _, _, _ = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1"), {"1": _gap_state(days_back_start=14)}
    )
    # Window 1 delivers nothing but advances the gap; window 2's fetch fails.
    get_positions.side_effect = [[]] + [httpx.ReadTimeout("")] * 4
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())

    result = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig()
    )

    assert result["observations_extracted"] == 0
    assert result["devices_failed"] == ["1"]
    assert result["zero_progress"] is True
    trigger.assert_not_awaited()


def _zero_progress_errors(log):
    return [
        c for c in log.await_args_list
        if c.kwargs.get("level") == LogLevel.ERROR
        and "No devices could be backfilled" in c.kwargs.get("title", "")
    ]


@pytest.mark.asyncio
async def test_breaker_stop_still_publishes_the_zero_progress_error(
    mocker, lotek_integration, mock_redis
):
    # Pins backfill's suppression policy (spec criterion 8: alerting unchanged).
    # ONLY connection-budget starvation excuses a no-progress backfill run. A
    # hot circuit breaker must NOT: a Lotek-wide outage is exactly when a
    # breaker stop and zero progress co-occur, and the cdip health metric counts
    # ERROR activity events — suppressing here would make the outage look
    # healthy. The shard's expression deliberately differs; unifying the two
    # (an earlier draft hoisted a shared `deferred_cleanly` onto the traversal)
    # would silently break this, so assert it rather than trust the expression.
    get_positions, _, _, _, log = _setup_backfill_mocks(
        mocker, mock_redis,
        _devices("1", "2", "3", "4", "5", "6"),
        {d: _gap_state() for d in ("1", "2", "3", "4", "5", "6")},
    )
    # Every fetch times out: chunk 1 (FETCH_CONCURRENCY=5) records 5 consecutive
    # transport failures, so should_stop() before chunk 2 returns
    # "circuit breaker" and defers device 6.
    get_positions.side_effect = httpx.ReadTimeout("")
    result = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig()
    )
    assert result["devices_failed"] == ["1", "2", "3", "4", "5"]
    assert result["devices_deferred"] == ["6"]  # the breaker really did stop it
    breaker_deferrals = [
        c for c in log.await_args_list
        if c.kwargs.get("level") == LogLevel.WARNING
        and "circuit breaker" in c.kwargs.get("title", "")
    ]
    assert breaker_deferrals, "the breaker stop must be the reason the tail deferred"
    assert result["zero_progress"] is True
    assert _zero_progress_errors(log), (
        "a breaker stop must NOT suppress the zero-progress ERROR"
    )


@pytest.mark.asyncio
async def test_deadline_stop_still_publishes_the_zero_progress_error(
    mocker, lotek_integration, mock_redis
):
    # Same policy, other stop reason: a deadline cut is not a clean back-off for
    # backfill (unlike the shard, it hands nothing off to a fresh budget), so it
    # must keep alerting too.
    get_positions, _, _, _, log = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1", "2"), {"1": _gap_state(), "2": _gap_state()}
    )
    mocker.patch("app.actions.handlers._deadline_exceeded", return_value=True)
    result = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig()
    )
    get_positions.assert_not_awaited()  # stopped before the first chunk
    assert result["devices_deferred"] == ["1", "2"]
    assert result["zero_progress"] is True
    assert _zero_progress_errors(log), (
        "a deadline stop must NOT suppress the zero-progress ERROR"
    )
