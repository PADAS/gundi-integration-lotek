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
    LotekTokenExpiredException,
    LotekUnauthorizedException, LotekPosition,
)


def _make_mock_client(response=None, raise_exc=None, method="post"):
    """
    Helper that returns an AsyncMock standing in for the shared httpx.AsyncClient
    (client.py uses a plain module-level client via _get_client(), not a context
    manager). If `raise_exc` is provided, the given method will raise it;
    otherwise it will return `response`.
    """
    mock_client = AsyncMock()
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
    exc = httpx.HTTPStatusError("400", request=resp.request, response=resp)
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

    # 401 with a cached token means the token expired, not that credentials were
    # refused; it must not carry the type that aborts the whole run.
    with pytest.raises(LotekTokenExpiredException):
        await get_devices(lotek_integration, auth_config)


@pytest.mark.asyncio
async def test_get_devices_http_error_raises_lotek_exception(mocker, lotek_integration, auth_config):
    exc = httpx.HTTPStatusError("500", request=httpx.Request("GET", lotek_integration.base_url), response=httpx.Response(500))
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
    with pytest.raises(LotekTokenExpiredException):
        await get_positions(1, auth_config, lotek_integration, from_date, to_date, True)


@pytest.mark.asyncio
async def test_get_positions_http_error_raises_lotek_exception(mocker, lotek_integration, auth_config):
    exc = httpx.HTTPStatusError("500", request=httpx.Request("GET", lotek_integration.base_url), response=httpx.Response(500))
    mock_client = _make_mock_client(raise_exc=exc, method="get")
    mocker.patch("app.actions.client.get_token", return_value="token")
    mocker.patch("app.actions.client.httpx.AsyncClient", return_value=mock_client)

    from_date = datetime.now(timezone.utc)
    to_date = datetime.now(timezone.utc)
    with pytest.raises(LotekException):
        await get_positions(1, auth_config, lotek_integration, from_date, to_date, True)


@pytest.mark.parametrize("status", [400, 401, 403])
@pytest.mark.asyncio
async def test_get_token_from_api_rejected_login_is_unauthorized(mocker, lotek_integration, auth_config, status):
    # Only a genuine credentials rejection should be Unauthorized, because callers
    # abort the whole run on it. Lotek answers a bad login with 400.
    resp = httpx.Response(status, json={"error": "invalid_grant"}, request=httpx.Request("POST", lotek_integration.base_url))
    mock_client = _make_mock_client(response=resp, method="post")
    mocker.patch("app.actions.client.httpx.AsyncClient", return_value=mock_client)

    with pytest.raises(LotekUnauthorizedException):
        await get_token_from_api(lotek_integration, auth_config)


@pytest.mark.parametrize("status", [429, 500, 502, 503])
@pytest.mark.asyncio
async def test_get_token_from_api_server_failure_is_not_unauthorized(mocker, lotek_integration, auth_config, status):
    # A Lotek outage or rate limit is not a credentials problem; reporting it as one
    # would tell operators their credentials are wrong when the server is just down.
    resp = httpx.Response(status, json={"error": "server"}, request=httpx.Request("POST", lotek_integration.base_url))
    mock_client = _make_mock_client(response=resp, method="post")
    mocker.patch("app.actions.client.httpx.AsyncClient", return_value=mock_client)

    with pytest.raises(LotekException) as excinfo:
        await get_token_from_api(lotek_integration, auth_config)
    assert not isinstance(excinfo.value, LotekUnauthorizedException)


@pytest.mark.asyncio
async def test_shared_client_is_constructed_once_and_reused(mocker, lotek_integration, auth_config):
    # The whole point of GUNDI-5620: hundreds of get_positions calls per run
    # must share ONE client (one connection pool, one set of TLS handshakes),
    # not build one each. Construction count is the observable contract.
    from app.actions import client as lotek_client

    resp_devices = httpx.Response(200, json=[], request=httpx.Request("GET", lotek_integration.base_url))
    resp_positions = httpx.Response(200, json=[], request=httpx.Request("GET", lotek_integration.base_url))
    mock_client = AsyncMock()
    mock_client.get.side_effect = [resp_devices, resp_positions, resp_positions]
    constructor = mocker.patch("app.actions.client.httpx.AsyncClient", return_value=mock_client)
    mocker.patch.object(lotek_client.state_manager, "get_state", AsyncMock(return_value={"token": "abc123"}))

    await get_devices(lotek_integration, auth_config)
    await get_positions(1, auth_config, lotek_integration)
    await get_positions(2, auth_config, lotek_integration)

    assert constructor.call_count == 1
    assert mock_client.get.call_count == 3


@pytest.mark.asyncio
async def test_close_client_closes_and_clears_the_singleton(mocker):
    from app.actions import client as lotek_client

    mock_client = AsyncMock()
    mocker.patch("app.actions.client.httpx.AsyncClient", return_value=mock_client)

    assert lotek_client._get_client() is mock_client
    await lotek_client.close_client()

    mock_client.aclose.assert_awaited_once()
    assert lotek_client._client is None
