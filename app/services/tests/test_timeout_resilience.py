import asyncio

import pytest
from unittest.mock import AsyncMock

from gundi_core.events import IntegrationActionFailed

from app.conftest import MockPullActionConfiguration
from app.services.activity_logger import activity_logger, log_action_activity
from app.services.action_runner import execute_action


def _failed_events(mock_publish_event):
    events = []
    for call in mock_publish_event.mock_calls:
        event = call.kwargs.get("event")
        if event is None and call.args:
            event = call.args[0]
        if isinstance(event, IntegrationActionFailed):
            events.append(event)
    return events


# --- Telemetry must be best-effort (GUNDI-5602 prod finding) ---------------
# Under PubSub egress congestion, publish_event exhausts its retries and
# re-raises asyncio.TimeoutError. Before the fix that escaped through the
# activity_logger decorator into execute_action's `except asyncio.TimeoutError`
# and every run died in ~90s labeled "Action 'pull_observations' timed out".


@pytest.mark.asyncio
async def test_log_action_activity_survives_publish_failure(mocker):
    mocker.patch(
        "app.services.activity_logger.publish_event",
        AsyncMock(side_effect=asyncio.TimeoutError()),
    )

    from gundi_core.schemas.v2 import LogLevel

    # Must not raise: an activity log that can't be published is dropped.
    await log_action_activity(
        integration_id="6cf5b0eb-4d5d-42e0-9d5c-8b4d9d545c9a",
        action_id="pull_observations",
        title="No positions fetched for device 12345",
        level=LogLevel.WARNING,
    )


@pytest.mark.asyncio
async def test_activity_logger_decorator_survives_publish_failures(
        mocker, integration_v2, pull_observations_config
):
    mocker.patch(
        "app.services.activity_logger.publish_event",
        AsyncMock(side_effect=asyncio.TimeoutError()),
    )

    @activity_logger()
    async def action_pull_observations(integration, action_config):
        return {"observations_extracted": 10}

    # on_start and on_completion publishes both fail; the action must still
    # run and its result must come back.
    result = await action_pull_observations(
        integration=integration_v2, action_config=pull_observations_config
    )

    assert result == {"observations_extracted": 10}


@pytest.mark.asyncio
async def test_activity_logger_decorator_preserves_handler_error_when_publish_fails(
        mocker, integration_v2, pull_observations_config
):
    mocker.patch(
        "app.services.activity_logger.publish_event",
        AsyncMock(side_effect=asyncio.TimeoutError()),
    )

    @activity_logger()
    async def action_pull_observations(integration, action_config):
        raise ValueError("malformed device payload")

    # The handler's real error must propagate, not the publish failure.
    with pytest.raises(ValueError, match="malformed device payload"):
        await action_pull_observations(
            integration=integration_v2, action_config=pull_observations_config
        )


@pytest.mark.asyncio
async def test_trigger_action_publish_failure_still_raises(mocker, integration_v2):
    """The command path is NOT best-effort: a lost RunIntegrationAction command
    means the triggered action never runs, so the caller must see the failure
    (handlers wrap their trigger_action calls in try/except for this)."""
    from app.services.action_scheduler import trigger_action
    from app.actions.configurations import BackfillObservationsConfig

    mocker.patch("app.services.action_scheduler.settings.TRIGGER_ACTIONS_ALWAYS_SYNC", False)
    mocker.patch("app.services.action_scheduler.settings.INTEGRATION_COMMANDS_TOPIC", "test-topic")
    mocker.patch(
        "app.services.action_scheduler.publish_event",
        AsyncMock(side_effect=asyncio.TimeoutError()),
    )

    with pytest.raises(asyncio.TimeoutError):
        await trigger_action(
            str(integration_v2.id), "backfill_observations",
            config=BackfillObservationsConfig(triggered_by="pull_observations"),
        )


# --- The runner must not conflate internal timeouts with its own deadline --


@pytest.mark.asyncio
async def test_internal_asyncio_timeout_is_not_labeled_as_action_deadline(
        mocker, mock_config_manager, mock_publish_event, integration_v2
):
    async def handler(integration, action_config):
        raise asyncio.TimeoutError()  # e.g. redis/aiohttp dependency timeout

    mocker.patch(
        "app.services.action_runner.action_handlers",
        {"pull_observations": (handler, MockPullActionConfiguration, None)},
    )
    mocker.patch("app.services.action_runner.config_manager", mock_config_manager)
    mocker.patch("app.services.activity_logger.publish_event", mock_publish_event)
    mocker.patch("app.services.action_runner._publish_activity_event", mock_publish_event)

    await execute_action(
        integration_id=str(integration_v2.id), action_id="pull_observations"
    )

    failed = _failed_events(mock_publish_event)
    assert failed, "an action failure event must still be published"
    error_text = failed[-1].payload.error
    # The handler died ~instantly, nowhere near the deadline — the error must
    # say so instead of masquerading as the wait_for ceiling.
    assert "deadline" in error_text
    assert f"Action 'pull_observations' timed out" not in error_text


@pytest.mark.asyncio
async def test_wait_for_ceiling_is_still_reported_as_timeout(
        mocker, mock_config_manager, mock_publish_event, integration_v2
):
    async def handler(integration, action_config):
        await asyncio.sleep(5)

    mocker.patch(
        "app.services.action_runner.action_handlers",
        {"pull_observations": (handler, MockPullActionConfiguration, None)},
    )
    mocker.patch("app.services.action_runner.config_manager", mock_config_manager)
    mocker.patch("app.services.activity_logger.publish_event", mock_publish_event)
    mocker.patch("app.services.action_runner._publish_activity_event", mock_publish_event)
    mocker.patch("app.services.action_runner.settings.MAX_ACTION_EXECUTION_TIME", 0.2)

    await execute_action(
        integration_id=str(integration_v2.id), action_id="pull_observations"
    )

    failed = _failed_events(mock_publish_event)
    assert failed
    assert "Action 'pull_observations' timed out" in failed[-1].payload.error


@pytest.mark.asyncio
async def test_handle_error_survives_publish_failure(
    mocker, mock_config_manager, integration_v2
):
    # Review finding: _handle_error's own IntegrationActionFailed publish went
    # through raising publish_event — under the exact congestion it reports
    # on, the publish timeout escaped execute_action, 500'd the route and
    # caused pubsub redelivery. It must be best-effort like other activity
    # events (the JSONResponse still carries error_details).
    from app.services import action_runner
    mocker.patch.object(action_runner, "config_manager", mock_config_manager)
    mocker.patch(
        "app.services.activity_logger.publish_event",
        AsyncMock(side_effect=asyncio.TimeoutError()),
    )
    response = await action_runner._handle_error(
        ValueError("boom"), integration_id=str(integration_v2.id), action_id="pull_observations"
    )
    assert response.status_code == 500
    assert b"boom" in response.body
