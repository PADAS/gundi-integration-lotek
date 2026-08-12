import pytest
import httpx
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from gundi_core.schemas.v2 import LogLevel
from app.actions.handlers import (
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
    assert result == {'observations_extracted': 0, 'devices_failed': []}
    mock_log_action_activity.assert_any_call(
        integration_id=str(lotek_integration.id),
        action_id="pull_observations",
        level=LogLevel.WARNING,
        title=f"No positions fetched for device {lotek_position.DeviceID} integration ID: {lotek_integration.id}."
    )

@pytest.mark.asyncio
async def test_lookback_days_config_sets_first_run_window(mocker, lotek_integration, pull_config, mock_redis):
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=[LotekDevice(nDeviceID="1", strSpecialID="special", dtCreated=datetime.now(), strSatellite="satellite")]))
    mock_get_positions = mocker.patch("app.actions.client.get_positions", new=AsyncMock(return_value=[]))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))
    pull_config.default_lookback_days = 30
    await action_pull_observations(lotek_integration, pull_config)
    # first chunk starts ~30 days back
    first_call_start = mock_get_positions.call_args_list[0].args[3]
    age_days = (datetime.now(timezone.utc) - first_call_start).days
    assert age_days == 30
    # window walked in 7-day chunks up to now
    assert len(mock_get_positions.call_args_list) == 5

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
    assert result == {'observations_extracted': 0, 'devices_failed': []}

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

    # Device 2 exhausts its retries and is given up on, but device 3 is still reached.
    assert queried[0] == "1"
    assert queried[-1] == "3"
    assert queried.count("2") == RETRY_ATTEMPTS
    assert result == {"observations_extracted": 2, "devices_failed": ["2"]}
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
    assert result == {"observations_extracted": 2, "devices_failed": ["2"]}


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
    assert result == {"observations_extracted": 2, "devices_failed": ["2"]}


@pytest.mark.asyncio
async def test_action_pull_observations_aborts_when_login_is_rejected(
    mocker, lotek_integration, pull_config, mock_redis
):
    # A rejected login is integration-wide. It reaches the handler from inside
    # get_positions (cached token expires mid-run), and must not be retried once per
    # device against a rejecting endpoint.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1", "2", "3")))
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

    assert len(logins) <= RETRY_ATTEMPTS, f"login retried per device: {len(logins)} attempts"


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

    assert result == {"observations_extracted": 1, "devices_failed": ["1"]}


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
async def test_action_pull_observations_keeps_successful_chunks_when_later_chunk_fails(
    mocker, lotek_integration, pull_config, mock_redis, lotek_position
):
    # With a lookback wider than one chunk, data already fetched should be delivered and
    # checkpointed up to the last good window instead of being thrown away every run.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mock_set_state = mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))
    mock_send = mocker.patch("app.services.gundi.send_observations_to_gundi", new=AsyncMock())
    pull_config.default_lookback_days = 30

    windows = []

    async def get_positions(device_id, auth, integration, lower, upper, geo_only):
        if lower not in windows:
            windows.append(lower)
        # the third window fails every time, so retries are exhausted on it
        if windows.index(lower) == 2:
            raise httpx.ReadTimeout("")
        return [lotek_position]

    mocker.patch("app.actions.client.get_positions", new=get_positions)

    result = await action_pull_observations(lotek_integration, pull_config)

    assert mock_send.call_count >= 1, "successful chunks were discarded"
    assert result["devices_failed"] == ["1"]
    # The cursor must land exactly on the end of the second (last successful) 7-day
    # window — i.e. ~16 days ago with a 30-day lookback — NOT at present_time, which
    # would silently skip the failed window forever.
    checkpoint = datetime.fromisoformat(mock_set_state.call_args_list[0].args[2]["updated_at"])
    expected = datetime.now(timezone.utc) - timedelta(days=30) + timedelta(days=14)
    assert abs((checkpoint - expected).total_seconds()) < 300, (
        f"cursor at {checkpoint}, expected the last successful window's end ~{expected}"
    )


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
    mocker, lotek_integration, pull_config, mock_redis
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

    titles = [call.kwargs["title"] for call in mock_log_action_activity.call_args_list]
    error_titles = [t for t in titles if "Error fetching positions" in t]
    assert error_titles, "expected a per-device error to be logged"
    assert "ReadTimeout" in error_titles[0]


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
    assert result == {"observations_extracted": 1, "devices_failed": []}


@pytest.mark.asyncio
async def test_action_pull_observations_aborts_on_auth_failure(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Bad credentials affect every device, so the run must stop instead of
    # repeating the same failure once per device.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=_devices("1", "2", "3")))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))

    queried = []

    async def get_positions(device_id, *args, **kwargs):
        queried.append(device_id)
        raise LotekUnauthorizedException(message="401 Response from Lotek API")

    mocker.patch("app.actions.client.get_positions", new=get_positions)

    with pytest.raises(LotekUnauthorizedException):
        await action_pull_observations(lotek_integration, pull_config)

    assert set(queried) == {"1"}, "should not have moved on to other devices"


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
