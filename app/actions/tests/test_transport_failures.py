import pytest

import httpx
from unittest.mock import AsyncMock

from app.actions.handlers import action_pull_observations, action_backfill_observations
from app.actions.configurations import BackfillObservationsConfig


def _patch_retry_waits(mocker):
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    # The @activity_logger decorator publishes to real PubSub otherwise.
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())


def _levels(mock_log_activity):
    from gundi_core.schemas.v2 import LogLevel

    levels = []
    for call in mock_log_activity.mock_calls:
        level = call.kwargs.get("level")
        if level is None:
            continue
        levels.append(level.upper() if isinstance(level, str) else LogLevel(int(level)).name)
    return levels


# GUNDI-5602 prod finding: fleet-wide Lotek congestion makes get_devices fail
# with httpx.ConnectTimeout on many integrations at once. That is a transport
# failure — the same class the per-device breaker treats as WARNING — but the
# get_devices error path classified it ERROR and re-raised, marking every
# connection unhealthy over a transient upstream condition.


@pytest.mark.asyncio
async def test_pull_get_devices_transport_failure_is_warning_and_clean_return(
        mocker, lotek_integration, pull_config
):
    _patch_retry_waits(mocker)
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch(
        "app.actions.client.get_devices",
        new=AsyncMock(side_effect=httpx.ConnectTimeout("connection timed out")),
    )
    mock_log = mocker.patch(
        "app.actions.handlers.log_action_activity", new=AsyncMock()
    )

    result = await action_pull_observations(lotek_integration, pull_config)

    assert result["skipped"] is True
    assert result["reason"] == "lotek_unreachable"
    assert result["shards_triggered"] == 0
    levels = _levels(mock_log)
    assert "WARNING" in levels
    assert "ERROR" not in levels


@pytest.mark.asyncio
async def test_pull_get_devices_non_transport_failure_stays_error_and_raises(
        mocker, lotek_integration, pull_config
):
    _patch_retry_waits(mocker)
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch(
        "app.actions.client.get_devices",
        new=AsyncMock(side_effect=ValueError("unexpected devices payload")),
    )
    mock_log = mocker.patch(
        "app.actions.handlers.log_action_activity", new=AsyncMock()
    )

    with pytest.raises(ValueError, match="unexpected devices payload"):
        await action_pull_observations(lotek_integration, pull_config)

    assert "ERROR" in _levels(mock_log)


@pytest.mark.asyncio
async def test_backfill_get_devices_transport_failure_is_warning_and_releases_lease(
        mocker, lotek_integration, mock_redis
):
    from app.actions.configurations import PullObservationsConfig

    _patch_retry_waits(mocker)
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch(
        "app.actions.client.get_devices",
        new=AsyncMock(side_effect=httpx.ConnectTimeout("connection timed out")),
    )
    mocker.patch("app.actions.handlers.get_pull_config", return_value=PullObservationsConfig())
    mocker.patch(
        "app.services.state.IntegrationStateManager.acquire_lease",
        new=AsyncMock(return_value="lease-token"),
    )
    release = mocker.patch(
        "app.services.state.IntegrationStateManager.release_lease",
        new=AsyncMock(return_value=True),
    )
    mock_log = mocker.patch(
        "app.actions.handlers.log_action_activity", new=AsyncMock()
    )

    result = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig(triggered_by="pull_observations")
    )

    assert result["skipped"] is True
    assert result["reason"] == "lotek_unreachable"
    release.assert_awaited()
    levels = _levels(mock_log)
    assert "WARNING" in levels
    assert "ERROR" not in levels
