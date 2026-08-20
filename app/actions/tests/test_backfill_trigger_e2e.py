import pytest

from datetime import datetime
from unittest.mock import AsyncMock

from gundi_core.schemas.v2 import IntegrationActionConfiguration
from app.actions.client import LotekDevice
from app.actions.configurations import PullObservationsShardConfig
from app.actions.handlers import (
    BACKFILL_TRIGGER_CLAIM_SOURCE,
    action_pull_observations,
    action_pull_observations_shard,
)
from app.services.state import IntegrationStateManager
from .test_handlers import _setup_pull_mocks


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
    mocker.patch("app.services.action_runner._publish_activity_event", new=AsyncMock())
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
    mocker.patch("app.services.state.IntegrationStateManager.release_lease", new=AsyncMock(return_value=True))
    lease = mocker.patch(
        "app.services.state.IntegrationStateManager.acquire_lease", new=AsyncMock(return_value="lease-token")
    )

    result = await action_pull_observations(lotek_integration, pull_config)

    assert lease.await_count == 1, (
        f"backfill_observations never ran — the trigger's config resolution "
        f"short-circuited it. pull_observations result: {result}"
    )


@pytest.mark.asyncio
async def test_claim_is_released_when_the_trigger_publish_fails(
    mocker, lotek_integration, pull_config, mock_redis
):
    """The claim (TTL 540s) is taken before the publish. If the publish fails,
    a stale claim suppresses backfill for every other shard this tick AND the
    next — pre-sharding a lost trigger self-healed on the next run. Roll back.

    Setup mirrors test_sharding.py::test_only_one_shard_triggers_backfill_per_window
    (a proven way to reach the any_open_gap-and-not-zero_progress branch: an
    unsaved device on its first run opens a gap from the lookback floor, and a
    quiet (positions=[]) fetch still counts as serviced, so zero_progress stays
    False). The only difference here is that trigger_action raises."""
    _setup_pull_mocks(mocker, mock_redis, [], saved_state=None)
    mocker.patch("app.actions.handlers.get_pull_config", return_value=pull_config)
    mocker.patch(
        "app.actions.handlers.trigger_action", side_effect=RuntimeError("pubsub down")
    )
    # set_if_absent is granted by an autouse fixture (conftest's
    # _grant_backfill_trigger_claim) and get_state returns {} (falsy, i.e. no
    # lease yet) via _setup_pull_mocks above, so the claim is won and the
    # publish is attempted. delete_state is also autouse-stubbed
    # (_stub_state_delete); re-patch it here so this test can assert on it.
    delete_state = mocker.patch.object(
        IntegrationStateManager, "delete_state", new=AsyncMock(return_value=None)
    )

    await action_pull_observations_shard(
        lotek_integration, PullObservationsShardConfig(devices=["1"])
    )

    delete_state.assert_awaited_once()
    assert delete_state.await_args.args[1] == "backfill_observations"
    assert delete_state.await_args.kwargs["source_id"] == BACKFILL_TRIGGER_CLAIM_SOURCE
