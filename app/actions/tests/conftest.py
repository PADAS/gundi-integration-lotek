import pytest

from app.services.state import IntegrationStateManager


@pytest.fixture(autouse=True)
def _emulate_atomic_state_merge(monkeypatch):
    # merge_state_fields is a server-side Lua script in production; tests mock
    # the class-level get_state/set_state, so emulate the script's
    # read-merge-write through them. Resolved at call time, so each test's own
    # get_state/set_state patches (including side_effect sequences that model
    # concurrent writers) are what the merge sees — and assertions keep
    # inspecting set_state's merged documents.
    from app.actions.handlers import state_manager

    async def fake_merge(integration_id, action_id, updates, source_id="no-source", init_only=None):
        # go through the instance so both mock styles work (class-attr
        # AsyncMocks and plain functions taking self)
        current = await state_manager.get_state(integration_id, action_id, source_id)
        merged = {**(current or {}), **updates}
        # init_only mirrors the Lua script's key-presence check: a key stored
        # as JSON null decodes to cjson.null (not Lua nil), so it counts as
        # PRESENT and is not overwritten — only a missing key accepts the value.
        for key, value in (init_only or {}).items():
            if key not in merged:
                merged[key] = value
        await state_manager.set_state(integration_id, action_id, merged, source_id)

    monkeypatch.setattr(IntegrationStateManager, "merge_state_fields", staticmethod(fake_merge))


@pytest.fixture(autouse=True)
def _reset_shared_http_client():
    # client.py caches one shared httpx.AsyncClient in a module global. Reset
    # it around every test so (a) a test's `mocker.patch(...httpx.AsyncClient)`
    # mock is never cached and leaked into later tests, and (b) no real client
    # outlives the pytest-asyncio event loop it was created on.
    import app.actions.client as lotek_client

    lotek_client._client = None
    yield
    lotek_client._client = None


@pytest.fixture(autouse=True)
def _zero_retry_waits(monkeypatch):
    # Retry-path tests otherwise sleep through real stamina backoff (minutes
    # over the suite, enough to blow a CI step timeout — review finding).
    # Zeroing the module constants keeps the retry logic itself exercised.
    monkeypatch.setattr("app.actions.handlers.RETRY_WAIT_INITIAL", 0.0)
    monkeypatch.setattr("app.actions.handlers.RETRY_WAIT_JITTER", 0.0)
    monkeypatch.setattr("app.actions.handlers.RETRY_WAIT_MAX", 0.0)
