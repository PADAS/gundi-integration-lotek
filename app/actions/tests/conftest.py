import pytest


@pytest.fixture(autouse=True)
def _zero_retry_waits(monkeypatch):
    # Retry-path tests otherwise sleep through real stamina backoff (minutes
    # over the suite, enough to blow a CI step timeout — review finding).
    # Zeroing the module constants keeps the retry logic itself exercised.
    monkeypatch.setattr("app.actions.handlers.RETRY_WAIT_INITIAL", 0.0)
    monkeypatch.setattr("app.actions.handlers.RETRY_WAIT_JITTER", 0.0)
    monkeypatch.setattr("app.actions.handlers.RETRY_WAIT_MAX", 0.0)
