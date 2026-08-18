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
    # Locks and login-rejection memos are also reset: a Lock binds to the
    # event loop of the first test that CONTENDS it, and pytest-asyncio runs
    # a fresh loop per test; a cached rejection would leak a fake auth
    # failure into unrelated tests.
    import app.actions.client as lotek_client

    lotek_client._client = None
    lotek_client._token_locks.clear()
    lotek_client._login_rejections.clear()
    yield
    lotek_client._client = None
    lotek_client._token_locks.clear()
    lotek_client._login_rejections.clear()


@pytest.fixture(autouse=True)
def _stub_state_delete(monkeypatch):
    # The dispatcher clears its slot-skip streak with delete_state on every
    # non-starved outcome. Most handler tests mock get_state/set_state but not
    # delete_state, so without this the call reaches a real Redis and burns
    # ~19s of stamina retries per test. Tests that assert on deletion patch it
    # themselves.
    from app.services.state import IntegrationStateManager
    from unittest.mock import AsyncMock

    monkeypatch.setattr(IntegrationStateManager, "delete_state", AsyncMock(return_value=None))


@pytest.fixture(autouse=True)
def _grant_backfill_trigger_claim(monkeypatch):
    # set_if_absent is the atomic "one backfill trigger per window" claim. It
    # is not part of the class-level get_state/set_state mocking most tests do,
    # so without this it reaches a real Redis (slow stamina retries). Granted
    # by default; the duplicate-suppression test overrides it.
    from app.services.state import IntegrationStateManager

    async def granted(self, integration_id, action_id, *, ttl_seconds, source_id="no-source"):
        return True

    monkeypatch.setattr(IntegrationStateManager, "set_if_absent", granted)


@pytest.fixture(autouse=True)
def _grant_connection_slots(monkeypatch):
    # lotek_slot talks to real Redis (per-account connection budget). Grant
    # every slot by default so handler tests don't need a Redis server; tests
    # exercising budget exhaustion patch app.actions.handlers.lotek_slot to
    # raise NoConnectionSlot themselves.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def granted_slot(username, **kwargs):
        yield

    monkeypatch.setattr("app.actions.handlers.lotek_slot", granted_slot)


@pytest.fixture(autouse=True)
def _zero_retry_waits(monkeypatch):
    # Retry-path tests otherwise sleep through real stamina backoff (minutes
    # over the suite, enough to blow a CI step timeout — review finding).
    # Zeroing the module constants keeps the retry logic itself exercised.
    monkeypatch.setattr("app.actions.handlers.RETRY_WAIT_INITIAL", 0.0)
    monkeypatch.setattr("app.actions.handlers.RETRY_WAIT_JITTER", 0.0)
    monkeypatch.setattr("app.actions.handlers.RETRY_WAIT_MAX", 0.0)
