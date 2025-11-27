import pytest
import httpx
from datetime import datetime
from unittest.mock import AsyncMock

from gundi_core.schemas.v2 import LogLevel
from app.actions.handlers import action_auth, filter_and_transform_positions, action_pull_observations
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
    assert result == {'observations_extracted': 0}
    mock_log_action_activity.assert_any_call(
        integration_id=str(lotek_integration.id),
        action_id="pull_observations",
        level=LogLevel.WARNING,
        title=f"No positions fetched for device {lotek_position.DeviceID} integration ID: {lotek_integration.id}."
    )

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
    assert result == {'observations_extracted': 0}

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
