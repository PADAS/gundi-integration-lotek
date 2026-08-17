import pytest
import httpx
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from gundi_core.schemas.v2 import LogLevel
from app.actions.handlers import (
    FETCH_CONCURRENCY,
    RETRY_ATTEMPTS,
    action_auth,
    action_pull_observations,
    filter_and_transform_positions,
)
from app.actions.client import LotekDevice, LotekException, LotekUnauthorizedException


@pytest.mark.asyncio
async def test_action_auth_success(mocker, lotek_integration, auth_config):
    mocker.patch("app.actions.client.get_token_from_api", new=AsyncMock(return_value="token"))
    result = await action_auth(lotek_integration, auth_config)
    assert result == {"valid_credentials": True}

@pytest.mark.asyncio
async def test_action_auth_invalid_credentials(mocker, lotek_integration, auth_config):
    mocker.patch("app.actions.client.get_token_from_api", new=AsyncMock(side_effect=LotekUnauthorizedException(error=Exception(), message="Invalid credentials")))
    result = await action_auth(lotek_integration, auth_config)
    assert result == {"valid_credentials": False, "message": "Invalid credentials"}

@pytest.mark.asyncio
async def test_action_auth_http_error(mocker, lotek_integration, auth_config):
    mocker.patch("app.actions.client.get_token_from_api", new=AsyncMock(side_effect=httpx.HTTPError("HTTP Error")))
    result = await action_auth(lotek_integration, auth_config)
    assert result == {"error": "An internal error occurred while trying to test credentials. Please try again later."}

def test_filter_and_transform_positions_success(mocker, lotek_position, lotek_integration):
    result = filter_and_transform_positions([lotek_position], lotek_integration)
    assert len(result) == 1
    assert result[0]["source"] == lotek_position.DeviceID
    assert result[0]["source_name"] == lotek_position.DevName
    assert result[0]["location"]["lat"] == lotek_position.Latitude
    assert result[0]["location"]["lon"] == lotek_position.Longitude

def test_filter_and_transform_positions_falls_back_to_device_id_for_blank_dev_name(mocker, lotek_position, lotek_integration):
    # Lotek's API can return an empty DevName; Gundi's sensors API rejects a blank source_name.
    lotek_position.DevName = ""
    result = filter_and_transform_positions([lotek_position], lotek_integration)
    assert result[0]["source_name"] == str(lotek_position.DeviceID)

def test_recorded_at_normalized_to_utc_for_non_utc_offset(lotek_position, lotek_integration):
    # A RecDateTime with a non-UTC offset must be converted to UTC, not
    # forwarded with its original offset (PR goal: UTC datetimes throughout).
    from datetime import timedelta
    lotek_position.RecDateTime = datetime(
        2026, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=2))
    )
    result = filter_and_transform_positions([lotek_position], lotek_integration)
    assert result[0]["recorded_at"] == "2026-01-01T10:00:00+00:00"


def test_recorded_at_assumes_utc_for_naive_datetime(lotek_position, lotek_integration):
    # A naive RecDateTime is assumed to already be UTC.
    lotek_position.RecDateTime = datetime(2026, 1, 1, 12, 0, 0)
    result = filter_and_transform_positions([lotek_position], lotek_integration)
    assert result[0]["recorded_at"] == "2026-01-01T12:00:00+00:00"


def test_filter_by_pdop_drops_positions_above_max(lotek_position, lotek_integration, pull_config):
    pull_config.max_pdop = 4.0
    lotek_position.PDOP = 4.1
    result = filter_and_transform_positions([lotek_position], lotek_integration, pull_config)
    assert result == []

def test_filter_by_pdop_keeps_positions_at_or_below_max(lotek_position, lotek_integration, pull_config):
    pull_config.max_pdop = 4.0
    lotek_position.PDOP = 4.0  # boundary: <= is kept
    result = filter_and_transform_positions([lotek_position], lotek_integration, pull_config)
    assert len(result) == 1
    assert result[0]["additional"]["PDOP"] == 4.0

def test_filter_by_pdop_disabled_by_default(lotek_position, lotek_integration, pull_config):
    assert pull_config.max_pdop is None
    lotek_position.PDOP = 99.9
    result = filter_and_transform_positions([lotek_position], lotek_integration, pull_config)
    assert len(result) == 1

def test_pdop_present_in_additional(lotek_position, lotek_integration, pull_config):
    result = filter_and_transform_positions([lotek_position], lotek_integration, pull_config)
    assert result[0]["additional"]["PDOP"] == lotek_position.PDOP

@pytest.mark.asyncio
async def test_invalid_position_filtered_and_logs_warning_when_no_valid_observations(mocker, lotek_position, lotek_integration, pull_config, mock_redis):
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=[LotekDevice(nDeviceID="1", strSpecialID="special", dtCreated=datetime.now(), strSatellite="satellite")]))
    # remove Latitude from lotek position
    lotek_position.Latitude = None
    mocker.patch("app.actions.client.get_positions", new=AsyncMock(return_value=[lotek_position]))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))
    mock_log_action_activity = mocker.patch("app.actions.handlers.log_action_activity", new=AsyncMock())
    result = await action_pull_observations(lotek_integration, pull_config)
    assert result == {'observations_extracted': 0, 'devices_failed': [], 'devices_deferred': []}
    # Publish-volume fix: quiet devices are local-log only — one portal WARNING
    # per dormant device was the largest pubsub-congestion contributor.
    quiet_publishes = [
        c for c in mock_log_action_activity.call_args_list
        if "No positions fetched" in c.kwargs.get("title", "")
    ]
    assert quiet_publishes == []

@pytest.mark.asyncio
async def test_lookback_days_config_sets_first_run_gap_depth(mocker, lotek_integration, pull_config, mock_redis):
    # default_lookback_days now controls only the first-run import depth: the
    # head pass makes ONE fresh call and the rest becomes the device's gap.
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=[LotekDevice(nDeviceID="1", strSpecialID="special", dtCreated=datetime.now(), strSatellite="satellite")]))
    mock_get_positions = mocker.patch("app.actions.client.get_positions", new=AsyncMock(return_value=[]))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mock_set_state = mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))
    mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    pull_config.default_lookback_days = 30
    await action_pull_observations(lotek_integration, pull_config)
    assert len(mock_get_positions.call_args_list) == 1
    saved = mock_set_state.call_args.args[2]
    gap_age_days = (datetime.now(timezone.utc) - saved["gap_start"]).days
    assert gap_age_days == 30

@pytest.mark.asyncio
async def test_action_pull_observations_success(mocker, lotek_integration, pull_config, mock_redis):
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=[LotekDevice(nDeviceID="1", strSpecialID="special", dtCreated=datetime.now(), strSatellite="satellite")]))
    mocker.patch("app.actions.client.get_positions", new=AsyncMock(return_value=[]))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))
    result = await action_pull_observations(lotek_integration, pull_config)
    assert result == {'observations_extracted': 0, 'devices_failed': [], 'devices_deferred': []}

def _devices(*device_ids):
    return [
        LotekDevice(nDeviceID=device_id, strSpecialID="special", dtCreated=datetime.now(), strSatellite="satellite")
        for device_id in device_ids
    ]


@pytest.mark.asyncio
async def test_action_pull_observations_continues_after_one_device_fails(
    mocker, lotek_integration, pull_config, mock_redis, lotek_position
):
    # A device whose positions can't be fetched must not stop the devices behind it.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1", "2", "3")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))
    mock_send = mocker.patch("app.services.gundi.send_observations_to_gundi", new=AsyncMock())

    queried = []

    async def get_positions(device_id, *args, **kwargs):
        queried.append(device_id)
        if device_id == "2":
            raise httpx.ReadTimeout("Lotek timed out")
        return [lotek_position]

    mocker.patch("app.actions.client.get_positions", new=get_positions)

    result = await action_pull_observations(lotek_integration, pull_config)

    # Device 2 exhausts its retries and is given up on, but devices 1 and 3 are
    # still serviced. Devices fetch concurrently within a chunk, so only the
    # per-device call counts are deterministic, not the interleaving.
    assert queried.count("1") == 1
    assert queried.count("3") == 1
    assert queried.count("2") == RETRY_ATTEMPTS
    assert result == {"observations_extracted": 2, "devices_failed": ["2"], "devices_deferred": []}
    assert mock_send.call_count == 2


@pytest.mark.asyncio
async def test_action_pull_observations_continues_after_lotek_error_status(
    mocker, lotek_integration, pull_config, mock_redis, lotek_position
):
    # get_positions raises LotekException (not an httpx error) for non-400/401 statuses.
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1", "2", "3")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))
    mocker.patch("app.services.gundi.send_observations_to_gundi", new=AsyncMock())

    queried = []

    async def get_positions(device_id, *args, **kwargs):
        queried.append(device_id)
        if device_id == "2":
            raise LotekException(message="Lotek get_positions failed", status_code=500)
        return [lotek_position]

    mocker.patch("app.actions.client.get_positions", new=get_positions)

    result = await action_pull_observations(lotek_integration, pull_config)

    assert queried == ["1", "2", "3"]
    assert result == {"observations_extracted": 2, "devices_failed": ["2"], "devices_deferred": []}


@pytest.mark.asyncio
async def test_action_pull_observations_continues_after_malformed_device_data(
    mocker, lotek_integration, pull_config, mock_redis, lotek_position
):
    # Lotek returning a malformed record raises out of the client's parsing (KeyError /
    # ValidationError), which is neither an httpx error nor a LotekException.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1", "2", "3")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))
    mocker.patch("app.services.gundi.send_observations_to_gundi", new=AsyncMock())

    queried = []

    async def get_positions(device_id, *args, **kwargs):
        queried.append(device_id)
        if device_id == "2":
            raise KeyError("Latitude")
        return [lotek_position]

    mocker.patch("app.actions.client.get_positions", new=get_positions)

    result = await action_pull_observations(lotek_integration, pull_config)

    assert queried[-1] == "3"
    assert result == {"observations_extracted": 2, "devices_failed": ["2"], "devices_deferred": []}


@pytest.mark.asyncio
async def test_action_pull_observations_aborts_when_login_is_rejected(
    mocker, lotek_integration, pull_config, mock_redis
):
    # A rejected login is integration-wide. It reaches the handler from inside
    # get_positions (cached token expires mid-run), and must not be retried once per
    # device against a rejecting endpoint. Devices fetch concurrently in chunks of
    # FETCH_CONCURRENCY, so the tightest stop the loop can offer is the chunk
    # boundary: at most one chunk's worth of devices may attempt a login before
    # the abort — later chunks must never dispatch.
    from app.actions.handlers import FETCH_CONCURRENCY
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1", "2", "3", "4", "5", "6", "7", "8")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))

    logins = []

    async def get_token_from_api(integration, auth):
        logins.append(auth.username)
        response = httpx.Response(400, request=httpx.Request("POST", "https://lotek/user/login"))
        raise LotekUnauthorizedException(
            message="Lotek login failed", error=httpx.HTTPStatusError("bad", request=response.request, response=response),
            status_code=400,
        )

    mocker.patch("app.actions.client.get_token_from_api", new=get_token_from_api)
    mocker.patch("app.services.state.IntegrationStateManager.delete_state", new=AsyncMock(return_value=None))

    with pytest.raises(LotekUnauthorizedException):
        await action_pull_observations(lotek_integration, pull_config)

    # The rejection memo in get_token means concurrent waiters re-raise the
    # cached refusal instead of each attempting a real login (lockout risk).
    assert len(logins) == 1, (
        f"a refused login must be attempted exactly once per run: {len(logins)} attempts"
    )


@pytest.mark.asyncio
async def test_action_pull_observations_continues_when_one_device_send_fails(
    mocker, lotek_integration, pull_config, mock_redis, lotek_position
):
    # Failing to deliver one device's observations must not stop the other devices
    # either — the batch is only as isolated as its least-guarded step.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1", "2")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mock_set_state = mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))
    mocker.patch("app.actions.client.get_positions", new=AsyncMock(return_value=[lotek_position]))

    sent_for = []

    async def send_observations_to_gundi(observations, integration_id):
        sent_for.append(observations[0]["source"])
        if len(sent_for) == 1:
            raise httpx.ConnectError("gundi unreachable")

    mocker.patch("app.services.gundi.send_observations_to_gundi", new=send_observations_to_gundi)

    result = await action_pull_observations(lotek_integration, pull_config)

    assert len(sent_for) == 2, "second device was never attempted"
    assert result["devices_failed"] == ["1"]
    # the failed device's cursor must stay put: checkpointing fetched-but-undelivered
    # data would silently skip it forever
    advanced = [call.args[-1] for call in mock_set_state.call_args_list]
    assert "1" not in advanced
    assert "2" in advanced


@pytest.mark.asyncio
async def test_action_pull_observations_counts_data_delivered_before_downstream_failure(
    mocker, lotek_integration, pull_config, mock_redis, lotek_position
):
    # A single device that DID deliver a batch and then failed at checkpointing must
    # not trip the "all devices failed and nothing delivered" raise: the data landed.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mocker.patch("app.services.state.IntegrationStateManager.set_state",
                 new=AsyncMock(side_effect=RuntimeError("redis down")))
    mocker.patch("app.actions.client.get_positions", new=AsyncMock(return_value=[lotek_position]))
    mocker.patch("app.services.gundi.send_observations_to_gundi", new=AsyncMock())

    result = await action_pull_observations(lotek_integration, pull_config)

    assert result == {"observations_extracted": 1, "devices_failed": ["1"], "devices_deferred": []}


@pytest.mark.asyncio
async def test_action_pull_observations_summary_names_failed_devices(
    mocker, lotek_integration, pull_config, mock_redis, lotek_position
):
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1", "2")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))
    mocker.patch("app.services.gundi.send_observations_to_gundi", new=AsyncMock())
    mock_log_action_activity = mocker.patch("app.actions.handlers.log_action_activity", new=AsyncMock())

    async def get_positions(device_id, *args, **kwargs):
        if device_id == "2":
            raise httpx.ReadTimeout("")
        return [lotek_position]

    mocker.patch("app.actions.client.get_positions", new=get_positions)

    await action_pull_observations(lotek_integration, pull_config)

    warnings = [c.kwargs["title"] for c in mock_log_action_activity.call_args_list
                if c.kwargs.get("level") == LogLevel.WARNING and "failing" in c.kwargs["title"]]
    assert warnings, "no failure summary was logged"
    assert "1 of 2" in warnings[0]
    assert "2" in warnings[0]


@pytest.mark.asyncio
async def test_action_pull_observations_continues_when_one_device_checkpoint_fails(
    mocker, lotek_integration, pull_config, mock_redis, lotek_position
):
    # A Redis hiccup while checkpointing one device must not abort the run.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1", "2")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mocker.patch("app.services.gundi.send_observations_to_gundi", new=AsyncMock())
    mocker.patch("app.actions.client.get_positions", new=AsyncMock(return_value=[lotek_position]))

    checkpointed = []

    async def set_state(self, integration_id, action_id, state, source_id="no-source"):
        checkpointed.append(source_id)
        if source_id == "1":
            raise RuntimeError("redis down")

    mocker.patch("app.services.state.IntegrationStateManager.set_state", new=set_state)

    result = await action_pull_observations(lotek_integration, pull_config)

    assert "2" in checkpointed, "second device was never processed"
    assert result["devices_failed"] == ["1"]


@pytest.mark.asyncio
async def test_action_pull_observations_fails_when_every_device_fails(
    mocker, lotek_integration, pull_config, mock_redis
):
    # A wholly broken integration must not report success, or the portal shows it green.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1", "2")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))
    mocker.patch("app.actions.client.get_positions", new=AsyncMock(side_effect=httpx.ReadTimeout("")))

    with pytest.raises(LotekException):
        await action_pull_observations(lotek_integration, pull_config)


@pytest.mark.asyncio
async def test_action_pull_observations_does_not_advance_state_for_failed_device(
    mocker, lotek_integration, pull_config, mock_redis, lotek_position
):
    # A failed device must re-query the same window next run, so its cursor stays put.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1", "2")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mock_set_state = mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))
    mocker.patch("app.services.gundi.send_observations_to_gundi", new=AsyncMock())

    async def get_positions(device_id, *args, **kwargs):
        if device_id == "2":
            raise httpx.ReadTimeout("Lotek timed out")
        return [lotek_position]

    mocker.patch("app.actions.client.get_positions", new=get_positions)

    await action_pull_observations(lotek_integration, pull_config)

    advanced_devices = [call.args[-1] for call in mock_set_state.call_args_list]
    assert "1" in advanced_devices
    assert "2" not in advanced_devices


@pytest.mark.asyncio
async def test_action_pull_observations_logs_exception_type_when_message_is_empty(
    mocker, lotek_integration, pull_config, mock_redis, caplog
):
    # httpx timeouts stringify to "", which produced logs ending in a bare "Exception: ".
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))
    mocker.patch("app.actions.client.get_positions", new=AsyncMock(side_effect=httpx.ReadTimeout("")))
    mock_log_action_activity = mocker.patch("app.actions.handlers.log_action_activity", new=AsyncMock())

    # the only device fails and delivers nothing, so the run is reported as failed
    with pytest.raises(LotekException):
        await action_pull_observations(lotek_integration, pull_config)

    # Transport failures are local-log only (publish-volume fix); the
    # exception type must still be named in the local warning.
    fetch_logs = [r.message for r in caplog.records if "Error fetching positions" in r.message]
    assert fetch_logs, "expected a per-device fetch failure to be logged locally"
    assert "ReadTimeout" in fetch_logs[0]
    published = [c.kwargs.get("title", "") for c in mock_log_action_activity.call_args_list]
    assert not any("Error fetching positions" in t for t in published)


@pytest.mark.asyncio
async def test_action_pull_observations_retries_transient_timeout(
    mocker, lotek_integration, pull_config, mock_redis, lotek_position
):
    # A single transient timeout should be retried, not treated as a dead device.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))
    mocker.patch("app.services.gundi.send_observations_to_gundi", new=AsyncMock())

    attempts = []

    async def get_positions(device_id, *args, **kwargs):
        attempts.append(device_id)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("")
        return [lotek_position]

    mocker.patch("app.actions.client.get_positions", new=get_positions)

    result = await action_pull_observations(lotek_integration, pull_config)

    assert len(attempts) == 2, "the transient timeout should have been retried"
    assert result == {"observations_extracted": 1, "devices_failed": [], "devices_deferred": []}


@pytest.mark.asyncio
async def test_action_pull_observations_aborts_on_auth_failure(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Bad credentials affect every device, so the run must stop instead of
    # repeating the same failure once per device. Devices fetch concurrently in
    # chunks of FETCH_CONCURRENCY, so the whole first chunk is already in flight
    # when the rejection surfaces — the guarantee is that no later chunk
    # dispatches.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1", "2", "3", "4", "5", "6", "7", "8")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))

    queried = []

    async def get_positions(device_id, *args, **kwargs):
        queried.append(device_id)
        raise LotekUnauthorizedException(message="401 Response from Lotek API")

    mocker.patch("app.actions.client.get_positions", new=get_positions)

    with pytest.raises(LotekUnauthorizedException):
        await action_pull_observations(lotek_integration, pull_config)

    first_chunk = {str(i) for i in range(1, FETCH_CONCURRENCY + 1)}
    assert set(queried) == first_chunk, (
        "should not have dispatched any chunk beyond the aborting one"
    )


@pytest.mark.asyncio
async def test_action_pull_observations_error(mocker, lotek_integration, pull_config, mock_redis):
    mock_log_action_activity = mocker.patch("app.actions.handlers.log_action_activity", new=AsyncMock())
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(side_effect=LotekException(error=Exception(), message="Lotek get_devices failed for user test_user.")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value=None))

    with pytest.raises(LotekException):
        await action_pull_observations(lotek_integration, pull_config)

    mock_log_action_activity.assert_called_with(
        integration_id=str(lotek_integration.id),
        action_id="pull_observations",
        level=LogLevel.ERROR,
        title=f"Error fetching devices from Lotek. Integration ID: {str(lotek_integration.id)} Exception: 500: Lotek get_devices failed for user test_user. | Error: "
    )


@pytest.mark.asyncio
async def test_action_pull_observations_isolates_persistent_401_on_one_device(
    mocker, lotek_integration, pull_config, mock_redis, lotek_position
):
    # A 401 that persists for one device after re-auth retries is a device problem
    # (or an auth flake), not proof of refused credentials — it must not abort the
    # run. Only a refused login (LotekUnauthorizedException from the login endpoint)
    # aborts. This is the GUNDI-5601 starvation bug via a third path.
    from app.actions.client import LotekTokenExpiredException
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1", "2", "3")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))
    mocker.patch("app.services.gundi.send_observations_to_gundi", new=AsyncMock())

    queried = []

    async def get_positions(device_id, *args, **kwargs):
        queried.append(device_id)
        if device_id == "2":
            raise LotekTokenExpiredException(message="401 Response from Lotek API")
        return [lotek_position]

    mocker.patch("app.actions.client.get_positions", new=get_positions)

    result = await action_pull_observations(lotek_integration, pull_config)

    assert "3" in queried, "devices after the 401 device were never queried"
    assert result["devices_failed"] == ["2"]


@pytest.mark.asyncio
async def test_action_pull_observations_does_not_retry_rejected_login_at_get_devices(
    mocker, lotek_integration, pull_config, mock_redis
):
    # A definitively refused login must fail immediately, not be retried with waits.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mock_log_action_activity = mocker.patch("app.actions.handlers.log_action_activity", new=AsyncMock())

    attempts = []

    async def get_devices(integration, auth):
        attempts.append(1)
        raise LotekUnauthorizedException(message="Lotek login failed", status_code=400)

    mocker.patch("app.actions.client.get_devices", new=get_devices)

    with pytest.raises(LotekUnauthorizedException):
        await action_pull_observations(lotek_integration, pull_config)

    assert len(attempts) == 1, f"refused login was retried {len(attempts)} times"


@pytest.mark.asyncio
async def test_action_pull_observations_quiet_device_unaffected_by_logging_failure(
    mocker, lotek_integration, pull_config, mock_redis
):
    # The "No positions fetched" entry is informational; a pubsub blip while logging
    # it must not mark a healthy quiet device as failed or stall its cursor.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mock_set_state = mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))
    mocker.patch("app.actions.client.get_positions", new=AsyncMock(return_value=[]))
    mocker.patch("app.actions.handlers.log_action_activity",
                 new=AsyncMock(side_effect=RuntimeError("pubsub down")))

    result = await action_pull_observations(lotek_integration, pull_config)

    assert result["devices_failed"] == []
    assert [c.args[-1] for c in mock_set_state.call_args_list] == ["1"], "cursor was not advanced"


@pytest.mark.asyncio
async def test_action_pull_observations_isolates_transform_failure_to_the_device(
    mocker, lotek_integration, pull_config, mock_redis, lotek_position
):
    # A malformed payload (transform blows up) is a per-device, fetch-class
    # failure: the device is marked failed and the devices behind it still run.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1", "2")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))
    mock_send = mocker.patch("app.services.gundi.send_observations_to_gundi", new=AsyncMock())

    async def get_positions(device_id, auth, integration, lower, upper, geo_only):
        if device_id == "1":
            return None  # malformed response: filter_and_transform will blow up iterating
        return [lotek_position]

    mocker.patch("app.actions.client.get_positions", new=get_positions)

    result = await action_pull_observations(lotek_integration, pull_config)

    assert result["devices_failed"] == ["1"]
    assert mock_send.call_count == 1, "the device behind the malformed one was not delivered"


@pytest.mark.asyncio
async def test_malformed_data_failure_logs_error_not_warning(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Review finding: malformed/unparseable data is a permanent, code/data-shape
    # problem — not Lotek being slow — and must stay ERROR so it can alert,
    # unlike a transient timeout (WARNING, see
    # test_transient_fetch_failure_logs_warning_not_error). The demotion to
    # WARNING was only ever meant for transient fetch timeouts.
    get_positions, _, _, log = _setup_pull_mocks(mocker, mock_redis, _devices("1", "2"))

    async def get_positions_side_effect(device_id, *args, **kwargs):
        return None if device_id == "1" else []  # device 1: malformed; device 2: fine

    get_positions.side_effect = get_positions_side_effect
    result = await action_pull_observations(lotek_integration, pull_config)
    assert result["devices_failed"] == ["1"]
    device_logs = [c for c in log.await_args_list if "Device: 1" in c.kwargs.get("title", "")]
    assert device_logs and all(c.kwargs["level"] == LogLevel.ERROR for c in device_logs)


@pytest.mark.asyncio
async def test_action_pull_observations_emits_summary_before_all_failed_raise(
    mocker, lotek_integration, pull_config, mock_redis
):
    # In the worst case (everything failed) the summary naming the devices is the
    # most valuable diagnostic — it must be emitted before the raise, not skipped.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1", "2")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))
    mocker.patch("app.actions.client.get_positions", new=AsyncMock(side_effect=httpx.ReadTimeout("")))
    mock_log_action_activity = mocker.patch("app.actions.handlers.log_action_activity", new=AsyncMock())

    with pytest.raises(LotekException):
        await action_pull_observations(lotek_integration, pull_config)

    warnings = [c.kwargs["title"] for c in mock_log_action_activity.call_args_list
                if c.kwargs.get("level") == LogLevel.WARNING and "failing" in c.kwargs["title"]]
    assert warnings, "summary was not emitted before the all-failed raise"
    assert "2 of 2" in warnings[0]


@pytest.mark.asyncio
async def test_action_auth_reports_server_error_as_internal_not_invalid_credentials(
    mocker, lotek_integration, auth_config
):
    # A Lotek 500 on login is not the operator's fault; saying "Invalid credentials"
    # sends them to reset a working password.
    mocker.patch("app.actions.client.get_token_from_api",
                 new=AsyncMock(side_effect=LotekException(message="login 500", status_code=500)))
    result = await action_auth(lotek_integration, auth_config)
    assert "error" in result
    assert result.get("valid_credentials") is not False


@pytest.mark.asyncio
async def test_action_auth_reports_rejected_login_as_invalid_credentials(
    mocker, lotek_integration, auth_config
):
    mocker.patch("app.actions.client.get_token_from_api",
                 new=AsyncMock(side_effect=LotekUnauthorizedException(message="login 400", status_code=400)))
    result = await action_auth(lotek_integration, auth_config)
    assert result == {"valid_credentials": False, "message": "Invalid credentials"}


@pytest.mark.asyncio
async def test_action_pull_observations_retries_token_expiry_and_recovers(
    mocker, lotek_integration, pull_config, mock_redis, lotek_position
):
    # A single token expiry must be retried (the client cleared the cached token, so
    # the retry re-authenticates), not treated as a device failure for the cycle.
    from app.actions.client import LotekTokenExpiredException
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))
    mocker.patch("app.services.gundi.send_observations_to_gundi", new=AsyncMock())

    attempts = []

    async def get_positions(device_id, *args, **kwargs):
        attempts.append(device_id)
        if len(attempts) == 1:
            raise LotekTokenExpiredException(message="401 Response from Lotek API")
        return [lotek_position]

    mocker.patch("app.actions.client.get_positions", new=get_positions)

    result = await action_pull_observations(lotek_integration, pull_config)

    assert len(attempts) == 2, "token expiry was not retried"
    assert result == {"observations_extracted": 1, "devices_failed": [], "devices_deferred": []}


@pytest.mark.asyncio
async def test_get_devices_failure_logs_exception_type_when_message_is_empty(
    mocker, lotek_integration, pull_config, mock_redis
):
    # httpx timeouts stringify to "" — the activity log must name the type,
    # not render a bare ": ". (Transport failures classify WARNING and return
    # cleanly since the 2026-08-16 congestion fix, so no raise here.)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch(
        "app.actions.client.get_devices",
        new=AsyncMock(side_effect=httpx.ReadTimeout("")),
    )
    mock_log = mocker.patch("app.actions.handlers.log_action_activity", new=AsyncMock())
    result = await action_pull_observations(lotek_integration, pull_config)
    assert result["reason"] == "lotek_unreachable"
    title = mock_log.call_args.kwargs["title"]
    assert "ReadTimeout" in title


def test_retry_attempts_is_two():
    # 3 attempts × 9 windows amplified Lotek's slowness into our own 9-min
    # timeouts (GUNDI-5602). One retry still recovers token expiry.
    assert RETRY_ATTEMPTS == 2


# --- GUNDI-5602 head-pass tests -------------------------------------------


def _setup_pull_mocks(mocker, mock_redis, devices, positions=None, saved_state=None):
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=devices))
    get_positions = mocker.patch(
        "app.actions.client.get_positions", new=AsyncMock(return_value=positions or [])
    )
    get_state = mocker.patch(
        "app.services.state.IntegrationStateManager.get_state",
        new=AsyncMock(return_value=saved_state or {}),
    )
    set_state = mocker.patch(
        "app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None)
    )
    mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    log = mocker.patch("app.actions.handlers.log_action_activity", new=AsyncMock())
    return get_positions, get_state, set_state, log


@pytest.mark.asyncio
async def test_head_pass_fetches_single_max_age_window_on_first_run(
    mocker, lotek_integration, pull_config, mock_redis
):
    # First run: ONE request per device covering [now - max_data_age_hours, now]
    # — not a chunked walk over the whole lookback.
    get_positions, _, _, _ = _setup_pull_mocks(mocker, mock_redis, _devices("1"))
    await action_pull_observations(lotek_integration, pull_config)
    assert get_positions.await_count == 1
    start, end = get_positions.call_args.args[3], get_positions.call_args.args[4]
    assert abs((end - start) - timedelta(hours=pull_config.max_data_age_hours)) < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_first_run_opens_gap_from_lookback_to_freshness_floor(
    mocker, lotek_integration, pull_config, mock_redis
):
    _, _, set_state, _ = _setup_pull_mocks(mocker, mock_redis, _devices("1"))
    await action_pull_observations(lotek_integration, pull_config)
    saved = set_state.call_args.args[2]
    gap_span = saved["gap_end"] - saved["gap_start"]
    expected = timedelta(days=pull_config.default_lookback_days) - timedelta(
        hours=pull_config.max_data_age_hours
    )
    assert abs(gap_span - expected) < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_no_gap_opened_when_lookback_fits_inside_max_age(mocker):
    # Portal bounds (lookback >= 1 day > max_age <= 12h) make this unreachable
    # via valid config, but the guard in _load_device_state must hold anyway —
    # pin it at the unit level with construct() to bypass validation.
    from app.actions.handlers import _load_device_state
    from app.actions.configurations import PullObservationsConfig
    mocker.patch(
        "app.services.state.IntegrationStateManager.get_state",
        new=AsyncMock(return_value={}),
    )
    config = PullObservationsConfig.construct(
        default_lookback_days=1, max_data_age_hours=48, max_pdop=None
    )
    state, is_new = await _load_device_state(
        "some-integration-id", "1", datetime.now(timezone.utc), config
    )
    assert is_new
    assert not state.has_gap
    assert state.gap_start is None and state.gap_end is None


@pytest.mark.asyncio
async def test_steady_state_advances_high_water_and_keeps_gap_closed(
    mocker, lotek_integration, pull_config, mock_redis
):
    # A device whose cursor is fresh (within max_age) head-fetches from its
    # cursor (minus the late-upload overlap) and neither opens a gap nor
    # drops anything.
    recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    get_positions, _, set_state, log = _setup_pull_mocks(
        mocker, mock_redis, _devices("1"), saved_state={"high_water": recent}
    )
    result = await action_pull_observations(lotek_integration, pull_config)
    start = get_positions.call_args.args[3]
    from app.actions.handlers import HEAD_LATE_UPLOAD_OVERLAP
    assert abs(
        (datetime.now(timezone.utc) - start) - (timedelta(hours=2) + HEAD_LATE_UPLOAD_OVERLAP)
    ) < timedelta(minutes=1)
    saved = set_state.call_args.args[2]
    assert saved.get("gap_start") is None
    assert abs(datetime.now(timezone.utc) - saved["high_water"]) < timedelta(minutes=1), (
        "high_water was not advanced to the queried upper bound"
    )
    assert result["devices_deferred"] == []
    warning_titles = [
        c.kwargs["title"] for c in log.await_args_list if c.kwargs["level"] == LogLevel.WARNING
    ]
    assert not any("stale" in t.lower() for t in warning_titles)


@pytest.mark.asyncio
async def test_stale_span_is_dropped_with_warning_and_not_added_to_gap(
    mocker, lotek_integration, pull_config, mock_redis, caplog
):
    # Bounded staleness (agreed design decision): a cursor further back than max_age
    # means that span is dropped permanently — WARNING with the range, gap unchanged.
    # The WARNING is published only after the cursor actually advances (review
    # finding: announcing it before the fetch misreported still-recoverable
    # data as dropped).
    stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    get_positions, _, set_state, log = _setup_pull_mocks(
        mocker, mock_redis, _devices("1"), saved_state={"high_water": stale}
    )
    await action_pull_observations(lotek_integration, pull_config)
    start = get_positions.call_args.args[3]
    assert abs(
        (datetime.now(timezone.utc) - start) - timedelta(hours=pull_config.max_data_age_hours)
    ) < timedelta(minutes=1)
    saved = set_state.call_args.args[2]
    assert saved.get("gap_start") is None  # NOT added to the gap
    # Per-device detail is local-log only (publish-volume fix), but permanent
    # loss stays portal-visible as ONE aggregated end-of-run summary.
    drop_warnings = [r.message for r in caplog.records if "Dropped stale range" in r.message]
    assert len(drop_warnings) == 1
    assert "device 1" in drop_warnings[0]
    assert not any("Dropped stale range" in c.kwargs.get("title", "") for c in log.await_args_list)
    summaries = [
        c for c in log.await_args_list
        if "Dropped data older than" in c.kwargs.get("title", "")
    ]
    assert len(summaries) == 1
    assert summaries[0].kwargs["level"] == LogLevel.WARNING
    assert "1" in summaries[0].kwargs["title"]


@pytest.mark.asyncio
async def test_legacy_updated_at_state_migrates_to_high_water(
    mocker, lotek_integration, pull_config, mock_redis
):
    recent = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    get_positions, _, set_state, _ = _setup_pull_mocks(
        mocker, mock_redis, _devices("1"), saved_state={"updated_at": recent}
    )
    await action_pull_observations(lotek_integration, pull_config)
    start = get_positions.call_args.args[3]
    from app.actions.handlers import HEAD_LATE_UPLOAD_OVERLAP
    assert abs(
        (datetime.now(timezone.utc) - start) - (timedelta(hours=3) + HEAD_LATE_UPLOAD_OVERLAP)
    ) < timedelta(minutes=1)
    saved = set_state.call_args.args[2]
    assert "high_water" in saved and saved.get("gap_start") is None


@pytest.mark.asyncio
async def test_transient_fetch_failure_logs_warning_not_error(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Health/alerting keys on ERROR count; recurring per-device timeouts must
    # not mark the connection unhealthy. devices_failed already tracks them.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    get_positions, _, set_state, log = _setup_pull_mocks(
        mocker, mock_redis, _devices("1", "2")
    )

    # Per-device behavior: chunked-concurrent fetching makes the call order
    # nondeterministic, so an ordered side_effect list would misfire.
    async def get_positions_side_effect(device_id, *args, **kwargs):
        if device_id == "1":
            raise httpx.ReadTimeout("")
        return []

    get_positions.side_effect = get_positions_side_effect
    result = await action_pull_observations(lotek_integration, pull_config)
    assert result["devices_failed"] == ["1"]
    # Transport failures are local-log only; the end-of-run summary is the
    # single portal publish and it is a WARNING, not an ERROR.
    device_1_publishes = [
        c for c in log.await_args_list if "Device: 1" in c.kwargs.get("title", "")
    ]
    assert device_1_publishes == []
    summaries = [
        c for c in log.await_args_list
        if "failing for integration" in c.kwargs.get("title", "")
    ]
    assert summaries and all(c.kwargs["level"] == LogLevel.WARNING for c in summaries)


@pytest.mark.asyncio
async def test_failed_head_fetch_does_not_advance_high_water(
    mocker, lotek_integration, pull_config, mock_redis
):
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    get_positions, _, set_state, _ = _setup_pull_mocks(
        mocker, mock_redis, _devices("1"), saved_state={"high_water": recent}
    )
    get_positions.side_effect = httpx.ReadTimeout("")
    with pytest.raises(LotekException):
        # single device, nothing serviced → the run-level failure raise
        await action_pull_observations(lotek_integration, pull_config)
    set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivery_failure_stays_error_and_does_not_advance_high_water(
    mocker, lotek_integration, lotek_position, pull_config, mock_redis
):
    get_positions, _, set_state, log = _setup_pull_mocks(
        mocker, mock_redis, _devices("1", "2"), positions=[lotek_position]
    )
    mocker.patch(
        "app.actions.handlers.gundi_tools.send_observations_to_gundi",
        new=AsyncMock(side_effect=[Exception("boom"), None]),
    )
    result = await action_pull_observations(lotek_integration, pull_config)
    assert result["devices_failed"] == ["1"]
    error_logs = [
        c for c in log.await_args_list
        if c.kwargs["level"] == LogLevel.ERROR and "delivering" in c.kwargs["title"].lower()
    ]
    assert len(error_logs) == 1
    # only device 2's checkpoint was written
    saved_devices = [c.args[3] for c in set_state.await_args_list]
    assert saved_devices == ["2"]


@pytest.mark.asyncio
async def test_all_failed_run_raises_zero_progress(
    mocker, lotek_integration, pull_config, mock_redis
):
    # A run that services nothing and delivers nothing is systemic degradation —
    # it must alert (raise/ERROR), not publish action_complete.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    get_positions, _, _, _ = _setup_pull_mocks(mocker, mock_redis, _devices("1", "2"))
    get_positions.side_effect = httpx.ReadTimeout("")
    with pytest.raises(LotekException, match="No devices could be serviced"):
        await action_pull_observations(lotek_integration, pull_config)


@pytest.mark.asyncio
async def test_zero_progress_run_raises_even_when_devices_were_only_deferred(
    mocker, lotek_integration, pull_config, mock_redis
):
    # A run that services nothing is systemic degradation — it must alert
    # (ERROR/raise), not warn forever. Deferring every device counts.
    _setup_pull_mocks(mocker, mock_redis, _devices("1", "2"))
    mocker.patch("app.actions.handlers._deadline_exceeded", return_value=True)
    with pytest.raises(LotekException, match="No devices could be serviced"):
        await action_pull_observations(lotek_integration, pull_config)


@pytest.mark.asyncio
async def test_deadline_defers_remaining_devices_with_warning(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Past ~80% of the action budget we stop STARTING device work and exit in
    # control — never via the asyncio.wait_for guillotine. The deadline is
    # checked at chunk boundaries (devices fetch concurrently in chunks of
    # FETCH_CONCURRENCY=5), so with 8 devices the calls are: should_stop()
    # before chunk 1, then _fetch_retry_kwargs() once per device in the chunk
    # (5), then should_stop() before chunk 2 — where the deadline hits and
    # devices 6-8 are deferred.
    import itertools
    _, _, _, log = _setup_pull_mocks(
        mocker, mock_redis, _devices("1", "2", "3", "4", "5", "6", "7", "8")
    )
    mocker.patch(
        "app.actions.handlers._deadline_exceeded",
        side_effect=itertools.chain([False] * 6, itertools.repeat(True)),
    )
    result = await action_pull_observations(lotek_integration, pull_config)
    assert result["devices_deferred"] == ["6", "7", "8"]
    assert result["devices_failed"] == []
    deferral_logs = [
        c for c in log.await_args_list
        if c.kwargs["level"] == LogLevel.WARNING and "deadline" in c.kwargs["title"].lower()
    ]
    assert len(deferral_logs) == 1


def test_retry_narrows_to_token_expiry_past_deadline(mocker):
    # Past the soft deadline transport retries stop (no budget for slow
    # backoff), but token expiry keeps its retry: the retry is a cheap
    # re-auth, and dropping it broke the "token expiry recovers within the
    # run" contract (review finding).
    from app.actions.handlers import _fetch_retry_kwargs, RETRYABLE_ERRORS
    from app.actions.client import LotekTokenExpiredException
    mocker.patch("app.actions.handlers._deadline_exceeded", return_value=False)
    assert _fetch_retry_kwargs(datetime.now(timezone.utc)) == {
        "on": RETRYABLE_ERRORS, "attempts": RETRY_ATTEMPTS
    }
    mocker.patch("app.actions.handlers._deadline_exceeded", return_value=True)
    assert _fetch_retry_kwargs(datetime.now(timezone.utc)) == {
        "on": LotekTokenExpiredException, "attempts": RETRY_ATTEMPTS
    }


def test_deadline_fraction_of_budget():
    # 540s budget → soft deadline ~432s. Pin the fraction.
    from app.actions.handlers import DEADLINE_FRACTION
    assert DEADLINE_FRACTION == 0.8


@pytest.mark.asyncio
async def test_breaker_trips_after_three_consecutive_transport_failures(
    mocker, lotek_integration, pull_config, mock_redis
):
    # 3+ consecutive timeouts = Lotek-wide degradation: stop early, defer the
    # rest (WARNING), instead of grinding every device into the same wall.
    # The breaker is checked at chunk boundaries (devices fetch concurrently in
    # chunks of FETCH_CONCURRENCY=5), so the whole first chunk runs — the
    # streak overshoots the threshold within the chunk — and deferral starts
    # at the next chunk: devices 6-8 are never dispatched.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    get_positions, _, _, log = _setup_pull_mocks(
        mocker, mock_redis, _devices("1", "2", "3", "4", "5", "6", "7", "8")
    )

    # device 1 succeeds; 2-5 exhaust retries on timeouts (streak 4 >= 3 at the
    # chunk boundary); 6-8 must be deferred
    async def get_positions_side_effect(device_id, *args, **kwargs):
        if device_id == "1":
            return []
        raise httpx.ReadTimeout("")

    get_positions.side_effect = get_positions_side_effect
    result = await action_pull_observations(lotek_integration, pull_config)
    assert result["devices_failed"] == ["2", "3", "4", "5"]
    assert result["devices_deferred"] == ["6", "7", "8"]
    breaker_logs = [
        c for c in log.await_args_list
        if c.kwargs["level"] == LogLevel.WARNING and "circuit breaker" in c.kwargs["title"].lower()
    ]
    assert len(breaker_logs) == 1


@pytest.mark.asyncio
async def test_breaker_counter_resets_on_success(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Failures interleaved with successes are per-device noise, not an outage.
    # Chunk results are recorded in list order, so chunk 1 (devices 1-5) plays
    # F S F S F: three failures but a max streak of 1 — without the reset the
    # streak would be 3 at the chunk boundary and device 6 would be deferred.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    get_positions, _, _, _ = _setup_pull_mocks(
        mocker, mock_redis, _devices("1", "2", "3", "4", "5", "6")
    )

    async def get_positions_side_effect(device_id, *args, **kwargs):
        if device_id in ("1", "3", "5"):
            raise httpx.ReadTimeout("")
        return []

    get_positions.side_effect = get_positions_side_effect
    result = await action_pull_observations(lotek_integration, pull_config)
    assert result["devices_deferred"] == []
    assert result["devices_failed"] == ["1", "3", "5"]


@pytest.mark.asyncio
async def test_head_pass_triggers_backfill_when_gap_open_and_lease_free(
    mocker, lotek_integration, pull_config, mock_redis
):
    # First run on default config opens a gap → backfill must be triggered.
    _setup_pull_mocks(mocker, mock_redis, _devices("1"))
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    await action_pull_observations(lotek_integration, pull_config)
    trigger.assert_awaited_once()
    args, kwargs = trigger.await_args
    assert args[:2] == (str(lotek_integration.id), "backfill_observations")
    # A fieldless config would serialize to an empty dict — indistinguishable
    # from "no override" and 404s in execute_action before the handler runs.
    config = kwargs.get("config") or args[2]
    assert config.dict() != {}


@pytest.mark.asyncio
async def test_head_pass_does_not_trigger_backfill_when_lease_held(
    mocker, lotek_integration, pull_config, mock_redis
):
    _setup_pull_mocks(mocker, mock_redis, _devices("1"))
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    # per-device state reads return {}, but the lease key reads as held
    mocker.patch(
        "app.services.state.IntegrationStateManager.get_state",
        new=AsyncMock(side_effect=lambda i, a, s="no-source": "1" if s == "lease" else {}),
    )
    await action_pull_observations(lotek_integration, pull_config)
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_head_pass_does_not_trigger_backfill_without_gaps(
    mocker, lotek_integration, pull_config, mock_redis
):
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _setup_pull_mocks(mocker, mock_redis, _devices("1"), saved_state={"high_water": recent})
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    await action_pull_observations(lotek_integration, pull_config)
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_trigger_failure_does_not_fail_the_head_pass(
    mocker, lotek_integration, pull_config, mock_redis
):
    _setup_pull_mocks(mocker, mock_redis, _devices("1"))
    mocker.patch(
        "app.actions.handlers.trigger_action", new=AsyncMock(side_effect=Exception("pubsub down"))
    )
    result = await action_pull_observations(lotek_integration, pull_config)
    assert result["devices_failed"] == []


def test_backfill_action_is_discovered_but_internal():
    from app.actions.core import discover_actions, InternalActionConfiguration
    handlers = discover_actions(module_name="app.actions.handlers", prefix="action_")
    assert "backfill_observations" in handlers
    _, config_model, _ = handlers["backfill_observations"]
    assert issubclass(config_model, InternalActionConfiguration)


# --- Fable-5 review fix round -----------------------------------------------


@pytest.mark.asyncio
async def test_stale_drop_publish_failure_does_not_stall_the_device(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Review finding: the stale-drop WARNING publish was the only activity
    # publish on the fetch path without a guard — a degraded pubsub must not
    # keep a stale device from ever advancing its cursor.
    stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    get_positions, _, set_state, log = _setup_pull_mocks(
        mocker, mock_redis, _devices("1"), saved_state={"high_water": stale}
    )

    async def flaky_publish(**kwargs):
        if "Dropped stale range" in kwargs.get("title", ""):
            raise Exception("pubsub down")

    log.side_effect = flaky_publish
    result = await action_pull_observations(lotek_integration, pull_config)
    assert result["devices_failed"] == []
    saved = set_state.call_args.args[2]
    assert abs(datetime.now(timezone.utc) - saved["high_water"]) < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_unparseable_state_reset_is_surfaced_at_warning(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Review finding: discarding a cursor and re-importing the whole lookback
    # was announced only at DEBUG. It must be visible in the activity log.
    get_positions, _, _, log = _setup_pull_mocks(
        mocker, mock_redis, _devices("1"), saved_state={"high_water": "not-a-date"}
    )
    await action_pull_observations(lotek_integration, pull_config)
    reset_warnings = [
        c for c in log.await_args_list
        if c.kwargs["level"] == LogLevel.WARNING and "unparseable" in c.kwargs["title"].lower()
    ]
    assert len(reset_warnings) == 1
    assert "device 1" in reset_warnings[0].kwargs["title"]


@pytest.mark.asyncio
async def test_head_pass_save_does_not_clobber_concurrently_closed_gap(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Review finding (lost-update race): the head pass must persist only the
    # fields it owns (high_water). If a concurrent backfill closed the gap
    # between our load and our save, the closed gap must survive our write.
    now = datetime.now(timezone.utc)
    open_gap_state = {
        "high_water": (now - timedelta(hours=2)).isoformat(),
        "gap_start": (now - timedelta(days=7)).isoformat(),
        "gap_end": (now - timedelta(hours=12)).isoformat(),
    }
    gap_closed_state = {
        "high_water": (now - timedelta(hours=2)).isoformat(),
        "gap_start": None,
        "gap_end": None,
        "last_backfilled": now.isoformat(),
    }
    get_positions, get_state, set_state, _ = _setup_pull_mocks(
        mocker, mock_redis, _devices("1")
    )
    # 1st read = the head pass loading its snapshot (gap open);
    # 2nd read = the merge-save re-read (backfill closed the gap meanwhile).
    get_state.side_effect = [open_gap_state, gap_closed_state]
    await action_pull_observations(lotek_integration, pull_config)
    saved = set_state.call_args.args[2]
    assert saved.get("gap_start") is None and saved.get("gap_end") is None, (
        "the head pass resurrected a gap a concurrent backfill had closed"
    )
    assert abs(datetime.now(timezone.utc) - saved["high_water"]) < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_stale_first_run_save_does_not_resurrect_a_gap_closed_meanwhile(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Review finding: two overlapping head runs both load is_new=True; the
    # faster one births the document and its triggered backfill closes the
    # gap (gap keys stored as null — present). The slower run's save must not
    # resurrect the gap from its stale snapshot: gap birth is create-only,
    # and a key present as null counts as present.
    now = datetime.now(timezone.utc)
    gap_closed_doc = {
        "version": 1,
        "high_water": (now - timedelta(minutes=5)).isoformat(),
        "gap_start": None,
        "gap_end": None,
        "last_backfilled": now.isoformat(),
    }
    _, get_state, set_state, _ = _setup_pull_mocks(mocker, mock_redis, _devices("1"))
    # 1st read = this run loading state (absent → is_new, gap birthed in memory);
    # 2nd read = the merge-save re-read (the faster run + backfill already ran).
    get_state.side_effect = [{}, gap_closed_doc]
    await action_pull_observations(lotek_integration, pull_config)
    saved = set_state.call_args.args[2]
    assert saved.get("gap_start") is None and saved.get("gap_end") is None, (
        "a stale is_new save resurrected a gap a backfill had already closed"
    )
    assert abs(datetime.now(timezone.utc) - saved["high_water"]) < timedelta(minutes=1), (
        "high_water must still be overwritten by the head save"
    )


@pytest.mark.asyncio
async def test_stale_first_run_save_does_not_rewind_an_advanced_gap(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Same race, gap still open: the faster run's backfill advanced gap_start
    # past some drained windows. The slower run's create-only gap birth must
    # leave the advanced value alone instead of rewinding to the full lookback.
    now = datetime.now(timezone.utc)
    advanced_start = (now - timedelta(days=3)).isoformat()
    gap_end = (now - timedelta(hours=12)).isoformat()
    gap_advanced_doc = {
        "version": 1,
        "high_water": (now - timedelta(minutes=5)).isoformat(),
        "gap_start": advanced_start,
        "gap_end": gap_end,
    }
    _, get_state, set_state, _ = _setup_pull_mocks(mocker, mock_redis, _devices("1"))
    get_state.side_effect = [{}, gap_advanced_doc]
    await action_pull_observations(lotek_integration, pull_config)
    saved = set_state.call_args.args[2]
    assert saved["gap_start"] == advanced_start, (
        "a stale is_new save rewound gap_start to its full-lookback snapshot"
    )
    assert saved["gap_end"] == gap_end


# --- chrisdoehring review fix round ------------------------------------------


@pytest.mark.asyncio
async def test_lotek_5xx_failures_feed_the_circuit_breaker(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Review finding: LotekException (e.g. a 503 during a Lotek-wide outage)
    # fell into the generic handler, which RESET the breaker streak — an
    # HTTP-error outage could never trip the breaker and the run ground
    # through every device. 5xx/429 must arm the breaker like timeouts.
    from app.actions.handlers import FETCH_CONCURRENCY
    get_positions, _, _, log = _setup_pull_mocks(
        mocker, mock_redis, _devices("1", "2", "3", "4", "5", "6", "7", "8")
    )
    get_positions.side_effect = LotekException(message="down", status_code=503)
    with pytest.raises(LotekException, match="No devices could be serviced"):
        await action_pull_observations(lotek_integration, pull_config)
    # the first chunk's 5 consecutive 503s trip the breaker at the chunk
    # boundary; devices 6-8 (chunk 2) are deferred and never dispatched
    deferral_logs = [
        c for c in log.await_args_list
        if "circuit breaker" in c.kwargs.get("title", "").lower()
    ]
    assert len(deferral_logs) == 1
    assert get_positions.await_count == FETCH_CONCURRENCY
    # Outage (5xx/429) failures are local-log only (publish-volume fix): no
    # per-device portal publishes, and in particular no ERRORs.
    device_logs = [c for c in log.await_args_list if "Device:" in c.kwargs.get("title", "")]
    assert device_logs == []


@pytest.mark.asyncio
async def test_lotek_4xx_failure_stays_error_and_does_not_feed_breaker(
    mocker, lotek_integration, pull_config, mock_redis
):
    # The 4xx side of the same finding: a per-device API-contract problem is
    # permanent — ERROR, and it must break (not feed) the breaker streak.
    get_positions, _, _, log = _setup_pull_mocks(
        mocker, mock_redis, _devices("1", "2", "3", "4")
    )
    get_positions.side_effect = LotekException(message="bad request", status_code=404)
    with pytest.raises(LotekException, match="No devices could be serviced"):
        await action_pull_observations(lotek_integration, pull_config)
    assert get_positions.await_count == 4, "4xx failures must not trip the breaker"
    device_logs = [c for c in log.await_args_list if "Device:" in c.kwargs.get("title", "")]
    assert device_logs and all(c.kwargs["level"] == LogLevel.ERROR for c in device_logs)


@pytest.mark.asyncio
async def test_head_pass_services_least_fresh_devices_first(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Review finding: deferral always cut the same stable-ordered tail, so
    # under sustained slowness the tail devices starved until bounded
    # staleness dropped their data permanently. The head pass now orders
    # least-fresh first (mirroring the backfill's LRS fairness).
    now = datetime.now(timezone.utc)
    states = {
        "fresh": {"high_water": (now - timedelta(minutes=10)).isoformat()},
        "stale": {"high_water": (now - timedelta(hours=10)).isoformat()},
        "middle": {"high_water": (now - timedelta(hours=5)).isoformat()},
    }
    get_positions, get_state, _, _ = _setup_pull_mocks(
        mocker, mock_redis, _devices("fresh", "stale", "middle")
    )
    get_state.side_effect = lambda i, a, d: states.get(d, {})
    await action_pull_observations(lotek_integration, pull_config)
    order = [c.args[0] for c in get_positions.await_args_list]
    assert order == ["stale", "middle", "fresh"]


@pytest.mark.asyncio
async def test_stale_legacy_cursor_migrates_into_gap_not_permanent_drop(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Review finding: on upgrade day a legacy cursor more than
    # max_data_age_hours behind got no gap — the owed range was dropped
    # permanently, though the pre-5602 walk would have recovered it. A stale
    # legacy cursor now carries its owed range over as the device's gap.
    behind = datetime.now(timezone.utc) - timedelta(days=3)
    get_positions, _, set_state, log = _setup_pull_mocks(
        mocker, mock_redis, _devices("1"), saved_state={"updated_at": behind.isoformat()}
    )
    result = await action_pull_observations(lotek_integration, pull_config)
    saved = set_state.call_args.args[2]
    assert abs(saved["gap_start"] - behind) < timedelta(seconds=1), (
        "the owed range's start must be the legacy cursor"
    )
    floor = datetime.now(timezone.utc) - timedelta(hours=pull_config.max_data_age_hours)
    assert abs(saved["gap_end"] - floor) < timedelta(minutes=1)
    assert abs(saved["high_water"] - datetime.now(timezone.utc)) < timedelta(minutes=1)
    # nothing was dropped, so no drop warning
    assert not any(
        "stale range" in c.kwargs.get("title", "").lower() for c in log.await_args_list
    )
    assert result["observations_extracted"] == 0


@pytest.mark.asyncio
async def test_no_stale_drop_warning_when_the_fetch_fails(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Review finding: the drop WARNING was published before the fetch, so a
    # failing device produced contradictory "permanently dropped" reports for
    # data that was still recoverable. No cursor advance → no drop warning.
    stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    get_positions, _, set_state, log = _setup_pull_mocks(
        mocker, mock_redis, _devices("1"), saved_state={"high_water": stale}
    )
    get_positions.side_effect = httpx.ReadTimeout("")
    with pytest.raises(LotekException, match="No devices could be serviced"):
        await action_pull_observations(lotek_integration, pull_config)
    set_state.assert_not_awaited()
    assert not any(
        "stale range" in c.kwargs.get("title", "").lower() for c in log.await_args_list
    )


@pytest.mark.asyncio
async def test_fetch_error_publish_failure_stays_contained_to_the_device(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Review finding: the activity publish inside _fetch_window's error
    # handlers was unguarded — a pubsub blip escaped the per-device isolation
    # and reset the breaker streak with a failure that says nothing about
    # Lotek. It must be best-effort like the other per-device publishes.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    get_positions, _, _, log = _setup_pull_mocks(
        mocker, mock_redis, _devices("1", "2")
    )

    # Per-device behavior: chunked-concurrent fetching makes the call order
    # nondeterministic. Device 1 times out through both retry attempts.
    async def get_positions_side_effect(device_id, *args, **kwargs):
        if device_id == "1":
            raise httpx.ReadTimeout("")
        return []

    get_positions.side_effect = get_positions_side_effect

    async def flaky_publish(**kwargs):
        if "Error fetching positions" in kwargs.get("title", ""):
            raise Exception("pubsub down")

    log.side_effect = flaky_publish
    result = await action_pull_observations(lotek_integration, pull_config)
    assert result["devices_failed"] == ["1"]
    assert get_positions.await_count == 3, "device 2 was never serviced"
