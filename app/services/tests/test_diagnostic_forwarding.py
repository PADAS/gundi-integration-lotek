import datetime

import pytest

from app.services import webhooks
from app.services.webhooks import (
    _validate_diagnostic_url,
    forward_payload_to_diagnostic_url,
)


def _patch_resolution(mocker, ip):
    """Force _validate_diagnostic_url's DNS lookup to resolve to `ip`.

    The validator calls asyncio.get_running_loop().getaddrinfo(...), and inside
    a running test that is the same loop the test coroutine runs on, so we can
    patch getaddrinfo on that instance.
    """
    import asyncio

    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(host, port):
        return [(None, None, None, "", (ip, 0))]

    mocker.patch.object(loop, "getaddrinfo", fake_getaddrinfo)


@pytest.mark.asyncio
async def test_validate_rejects_non_https():
    with pytest.raises(ValueError, match="scheme"):
        await _validate_diagnostic_url("http://diagnostics.example.com/dump")


@pytest.mark.asyncio
async def test_validate_rejects_missing_hostname():
    with pytest.raises(ValueError, match="no hostname"):
        await _validate_diagnostic_url("https:///dump")


@pytest.mark.asyncio
async def test_validate_rejects_host_not_in_allowlist(mocker):
    mocker.patch(
        "app.services.webhooks.settings.DIAGNOSTIC_URL_ALLOWLIST",
        ["allowed.example.com"],
    )
    with pytest.raises(ValueError, match="allowlist"):
        await _validate_diagnostic_url("https://other.example.com/dump")


@pytest.mark.asyncio
async def test_validate_rejects_private_address(mocker):
    mocker.patch("app.services.webhooks.settings.DIAGNOSTIC_URL_ALLOWLIST", [])
    _patch_resolution(mocker, "10.0.0.5")
    with pytest.raises(ValueError, match="private or reserved"):
        await _validate_diagnostic_url("https://sneaky.example.com/dump")


@pytest.mark.asyncio
async def test_validate_rejects_metadata_address(mocker):
    # The cloud metadata endpoint (169.254.169.254) is a classic SSRF target.
    mocker.patch("app.services.webhooks.settings.DIAGNOSTIC_URL_ALLOWLIST", [])
    _patch_resolution(mocker, "169.254.169.254")
    with pytest.raises(ValueError, match="private or reserved"):
        await _validate_diagnostic_url("https://metadata.example.com/dump")


@pytest.mark.asyncio
async def test_validate_allows_public_https(mocker):
    mocker.patch("app.services.webhooks.settings.DIAGNOSTIC_URL_ALLOWLIST", [])
    _patch_resolution(mocker, "93.184.216.34")  # public address
    # Should not raise.
    await _validate_diagnostic_url("https://diagnostics.example.com/dump")


@pytest.mark.asyncio
async def test_forward_posts_payload_with_metadata(mocker):
    # Happy path: exercises the real metadata-building code, including the
    # `received_at` timestamp — this is the path that used the 3.11-only
    # datetime.UTC and must work on the project's Python 3.10.
    mocker.patch(
        "app.services.webhooks._validate_diagnostic_url",
        mocker.AsyncMock(return_value=None),
    )
    mock_response = mocker.MagicMock(status_code=200)
    mock_client = mocker.MagicMock()
    mock_client.post = mocker.AsyncMock(return_value=mock_response)
    mocker.patch("app.services.webhooks._get_diagnostic_client", return_value=mock_client)

    await forward_payload_to_diagnostic_url(
        destination_url="https://diagnostics.example.com/dump",
        integration_id="abc-123",
        json_content={"device": "collar-1", "lat": 1.0},
    )

    mock_client.post.assert_awaited_once()
    _, kwargs = mock_client.post.call_args
    body = kwargs["json"]
    assert body["device"] == "collar-1"
    metadata = body["__gundi_diagnostic_metadata"]
    assert metadata["integration_id"] == "abc-123"
    # received_at is a valid, timezone-aware ISO-8601 timestamp.
    parsed = datetime.datetime.fromisoformat(metadata["received_at"])
    assert parsed.tzinfo is not None


@pytest.mark.asyncio
async def test_forward_wraps_non_dict_payload(mocker):
    mocker.patch(
        "app.services.webhooks._validate_diagnostic_url",
        mocker.AsyncMock(return_value=None),
    )
    mock_response = mocker.MagicMock(status_code=200)
    mock_client = mocker.MagicMock()
    mock_client.post = mocker.AsyncMock(return_value=mock_response)
    mocker.patch("app.services.webhooks._get_diagnostic_client", return_value=mock_client)

    await forward_payload_to_diagnostic_url(
        destination_url="https://diagnostics.example.com/dump",
        integration_id="abc-123",
        json_content=[{"a": 1}, {"b": 2}],  # a list, not a dict
    )

    _, kwargs = mock_client.post.call_args
    body = kwargs["json"]
    assert body["payload"] == [{"a": 1}, {"b": 2}]
    assert "__gundi_diagnostic_metadata" in body


@pytest.mark.asyncio
async def test_forward_swallows_validation_error(mocker):
    # Forwarding is best-effort: a failed SSRF check must not raise (which would
    # surface into the request/background task), and must not attempt the POST.
    mocker.patch(
        "app.services.webhooks._validate_diagnostic_url",
        mocker.AsyncMock(side_effect=ValueError("blocked")),
    )
    mock_client = mocker.MagicMock()
    mock_client.post = mocker.AsyncMock()
    mocker.patch("app.services.webhooks._get_diagnostic_client", return_value=mock_client)

    # Should not raise.
    await forward_payload_to_diagnostic_url(
        destination_url="https://sneaky.example.com/dump",
        integration_id="abc-123",
        json_content={"x": 1},
    )
    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_forward_swallows_http_error(mocker):
    mocker.patch(
        "app.services.webhooks._validate_diagnostic_url",
        mocker.AsyncMock(return_value=None),
    )
    mock_client = mocker.MagicMock()
    mock_client.post = mocker.AsyncMock(side_effect=Exception("connection refused"))
    mocker.patch("app.services.webhooks._get_diagnostic_client", return_value=mock_client)

    # Should not raise.
    await forward_payload_to_diagnostic_url(
        destination_url="https://diagnostics.example.com/dump",
        integration_id="abc-123",
        json_content={"x": 1},
    )


@pytest.mark.asyncio
async def test_process_webhook_schedules_diagnostic_forward(
    mocker, integration_v2_with_diagnostic_webhook,
    mock_get_webhook_handler_for_generic_json_payload, mock_webhook_handler,
    mock_webhook_request_headers_onyesha, mock_webhook_request_payload_for_dynamic_schema,
):
    # End-to-end: a webhook whose config carries diagnostic_destination_url
    # schedules a background forward and retains a strong reference to the task.
    mocker.patch(
        "app.services.webhooks.get_webhook_handler",
        mock_get_webhook_handler_for_generic_json_payload,
    )
    mocker.patch(
        "app.services.config_manager.IntegrationConfigurationManager.get_integration_details",
        mocker.AsyncMock(return_value=integration_v2_with_diagnostic_webhook),
    )
    forward = mocker.patch(
        "app.services.webhooks.forward_payload_to_diagnostic_url",
        mocker.AsyncMock(return_value=None),
    )
    webhooks._background_tasks.clear()

    from app.services.webhooks import process_webhook
    from fastapi import Request

    request = Request({"type": "http", "method": "POST", "url": "http://test/webhooks"})
    request._headers = mock_webhook_request_headers_onyesha
    request._query_params = {}
    mocker.patch.object(
        request, "json",
        mocker.AsyncMock(return_value=mock_webhook_request_payload_for_dynamic_schema),
    )

    await process_webhook(request)

    forward.assert_called_once()
    assert forward.call_args.kwargs["destination_url"] == (
        "https://diagnostics.example.com/webhook-dump"
    )
