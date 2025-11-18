import pytest
import httpx
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from app.actions.client import (
    get_token_from_api,
    get_devices,
    get_positions,
    LotekDevice,
    LotekException,
    LotekUnauthorizedException, LotekPosition,
)


def _make_mock_client(response=None, raise_exc=None, method="post"):
    """
    Helper that returns an AsyncMock usable as `httpx.AsyncClient` context manager.
    If `raise_exc` is provided, the given method will raise it; otherwise it will
    return `response`.
    """
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    if raise_exc is not None:
        getattr(mock_client, method).side_effect = raise_exc
    else:
        getattr(mock_client, method).return_value = response
    return mock_client


@pytest.mark.asyncio
async def test_get_token_from_api_success(mocker, lotek_integration, auth_config):
    resp = httpx.Response(200, json={"access_token": "abc123"}, request=httpx.Request("POST", lotek_integration.base_url))
    mock_client = _make_mock_client(response=resp, method="post")
    mocker.patch("app.actions.client.httpx.AsyncClient", return_value=mock_client)

    token = await get_token_from_api(lotek_integration, auth_config)
    assert token == "abc123"


@pytest.mark.asyncio
async def test_get_token_from_api_bad_credentials_raises_lotek_exception(mocker, lotek_integration, auth_config):
    resp = httpx.Response(400, request=httpx.Request("POST", lotek_integration.base_url))
    exc = httpx.HTTPStatusError("401", request=resp.request, response=resp)
    mock_client = _make_mock_client(raise_exc=exc, method="post")
    mocker.patch("app.actions.client.httpx.AsyncClient", return_value=mock_client)

    with pytest.raises(LotekException):
        await get_token_from_api(lotek_integration, auth_config)


@pytest.mark.asyncio
async def test_get_token_from_api_http_error_raises_lotek_exception(mocker, lotek_integration, auth_config):
    # Simulate a generic HTTPX error -> expect LotekException
    exc = httpx.HTTPStatusError("500", request=httpx.Request("POST", lotek_integration.base_url), response=httpx.Response(500))
    mock_client = _make_mock_client(raise_exc=exc, method="post")
    mocker.patch("app.actions.client.httpx.AsyncClient", return_value=mock_client)

    with pytest.raises(LotekException):
        await get_token_from_api(lotek_integration, auth_config)


@pytest.mark.asyncio
async def test_get_devices_success_returns_list(mocker, lotek_integration, auth_config):
    # Simulate successful devices response as JSON array
    payload = [
        {"nDeviceID": "1", "strSpecialID": "special", "dtCreated": datetime.now(timezone.utc).isoformat(), "strSatellite": "sat"}
    ]
    resp = httpx.Response(200, json=payload, request=httpx.Request("GET", lotek_integration.base_url))
    mock_client = _make_mock_client(response=resp, method="get")
    mocker.patch("app.actions.client.get_token", return_value="token")
    mocker.patch("app.actions.client.httpx.AsyncClient", return_value=mock_client)

    # Many implementations accept (auth_token, integration) or similar; use the integration fixture and a dummy token.
    result = await get_devices(lotek_integration, auth_config)
    assert isinstance(result, list)
    assert isinstance(result[0], LotekDevice)


@pytest.mark.asyncio
async def test_get_devices_unauthorized_raises(mocker, lotek_integration, auth_config):
    resp = httpx.Response(401, request=httpx.Request("GET", lotek_integration.base_url))
    exc = httpx.HTTPStatusError("401", request=resp.request, response=resp)
    mock_client = _make_mock_client(raise_exc=exc, method="get")
    mocker.patch("app.actions.client.get_token", return_value="token")
    mocker.patch("app.actions.client.state_manager.delete_state", return_value=None)
    mocker.patch("app.actions.client.httpx.AsyncClient", return_value=mock_client)

    with pytest.raises(LotekUnauthorizedException):
        await get_devices(lotek_integration, auth_config)


@pytest.mark.asyncio
async def test_get_devices_http_error_raises_lotek_exception(mocker, lotek_integration, auth_config):
    exc = httpx.HTTPStatusError("500", request=httpx.Request("POST", lotek_integration.base_url), response=httpx.Response(500))
    mock_client = _make_mock_client(raise_exc=exc, method="get")
    mocker.patch("app.actions.client.get_token", return_value="token")
    mocker.patch("app.actions.client.httpx.AsyncClient", return_value=mock_client)

    with pytest.raises(LotekException):
        await get_devices(lotek_integration, auth_config)


@pytest.mark.asyncio
async def test_get_positions_success_returns_list(mocker, lotek_integration, lotek_position, auth_config):
    resp = httpx.Response(200, json=[json.loads(lotek_position.json())], request=httpx.Request("GET", lotek_integration.base_url))
    mock_client = _make_mock_client(response=resp, method="get")
    mocker.patch("app.actions.client.get_token", return_value="token")
    mocker.patch("app.actions.client.httpx.AsyncClient", return_value=mock_client)

    # Use plausible args: device_id, auth_token, integration, from_date, to_date, include_fix
    from_date = datetime.now(timezone.utc)
    to_date = datetime.now(timezone.utc)
    result = await get_positions(1, auth_config, lotek_integration, from_date, to_date, True)
    assert isinstance(result, list)
    assert isinstance(result[0], LotekPosition)


@pytest.mark.asyncio
async def test_get_positions_unauthorized_raises(mocker, lotek_integration, auth_config):
    resp = httpx.Response(401, request=httpx.Request("GET", lotek_integration.base_url))
    exc = httpx.HTTPStatusError("401", request=resp.request, response=resp)
    mock_client = _make_mock_client(raise_exc=exc, method="get")
    mocker.patch("app.actions.client.get_token", return_value="token")
    mocker.patch("app.actions.client.state_manager.delete_state", return_value=None)
    mocker.patch("app.actions.client.httpx.AsyncClient", return_value=mock_client)

    from_date = datetime.now(timezone.utc)
    to_date = datetime.now(timezone.utc)
    with pytest.raises(LotekUnauthorizedException):
        await get_positions(1, auth_config, lotek_integration, from_date, to_date, True)


@pytest.mark.asyncio
async def test_get_positions_http_error_raises_lotek_exception(mocker, lotek_integration, auth_config):
    exc = httpx.HTTPStatusError("500", request=httpx.Request("POST", lotek_integration.base_url), response=httpx.Response(500))
    mock_client = _make_mock_client(raise_exc=exc, method="get")
    mocker.patch("app.actions.client.get_token", return_value="token")
    mocker.patch("app.actions.client.httpx.AsyncClient", return_value=mock_client)

    from_date = datetime.now(timezone.utc)
    to_date = datetime.now(timezone.utc)
    with pytest.raises(LotekException):
        await get_positions(1, auth_config, lotek_integration, from_date, to_date, True)
