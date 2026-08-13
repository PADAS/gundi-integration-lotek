import pytest

from datetime import datetime
from unittest.mock import AsyncMock

from gundi_core.schemas.v2 import IntegrationActionConfiguration
from app.actions.client import LotekDevice
from app.actions.handlers import action_pull_observations


@pytest.mark.asyncio
async def test_pull_observations_trigger_actually_runs_backfill_end_to_end(mocker, lotek_integration, pull_config):
    """Regression test (GUNDI-5602 review finding): backfill_observations is an
    InternalActionConfiguration, so no IntegrationActionConfiguration row for it
    is ever persisted (self_registration.py skips internal actions at
    registration; nothing else creates one). trigger_action() with no `config`
    argument publishes config_overrides=None, and execute_action's
    `not action_config and not config_overrides` check 404s BEFORE
    action_backfill_observations ever runs. This exercises the REAL chain —
    action_pull_observations -> trigger_action (sync) -> execute_action ->
    action_backfill_observations — which every other backfill test bypasses by
    calling action_backfill_observations directly, and which is why this was
    invisible until reviewed.
    """
    # Real Lotek integrations always carry a pull_observations config; add one
    # so get_pull_config() inside action_backfill_observations resolves it,
    # matching production (lotek_integration only ships an auth config).
    lotek_integration.configurations.append(
        IntegrationActionConfiguration.parse_obj({
            "id": "30f8878c-4a98-4c95-88eb-79f73c40fb2e",
            "integration": str(lotek_integration.id),
            "action": {
                "id": "75b3040f-ab1f-42e7-b39f-8965c088b154",
                "type": "pull",
                "name": "Pull Observations",
                "value": "pull_observations",
            },
            "data": {},
        })
    )
    mocker.patch("app.services.action_scheduler.settings.TRIGGER_ACTIONS_ALWAYS_SYNC", True)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.services.action_runner.publish_event", new=AsyncMock())
    mock_config_manager = mocker.patch("app.services.action_runner.config_manager")
    mock_config_manager.get_integration_details = AsyncMock(return_value=lotek_integration)
    # No stored config for the internal action — this is the actual production
    # state, since it is never registered or persisted anywhere.
    mock_config_manager.get_action_configuration = AsyncMock(return_value=None)

    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch(
        "app.actions.client.get_devices",
        new=AsyncMock(return_value=[LotekDevice(nDeviceID="1", strSpecialID="s", dtCreated=datetime.now(), strSatellite="sat")]),
    )
    mocker.patch("app.actions.client.get_positions", new=AsyncMock(return_value=[]))
    mocker.patch("app.services.state.IntegrationStateManager.get_state", new=AsyncMock(return_value={}))
    mocker.patch("app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None))
    mocker.patch("app.services.state.IntegrationStateManager.delete_state", new=AsyncMock(return_value=None))
    lease = mocker.patch(
        "app.services.state.IntegrationStateManager.set_if_absent", new=AsyncMock(return_value=True)
    )

    result = await action_pull_observations(lotek_integration, pull_config)

    assert lease.await_count == 1, (
        f"backfill_observations never ran — the trigger's config resolution "
        f"short-circuited it. pull_observations result: {result}"
    )
