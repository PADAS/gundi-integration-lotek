# Lotek Head Pass + Internal Backfill (GUNDI-5602) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Flip the Lotek connector to newest-first fetching: a head pass in `pull_observations` that always delivers the freshest window per device, plus an internal `backfill_observations` action that closes the bounded historical gap — with deadline / circuit-breaker / zero-progress / lease safety rails.

**Architecture:** Per the approved spec `docs/superpowers/specs/2026-08-13-lotek-head-pass-design.md` (source of truth). Per-device state moves from a single `updated_at` cursor to `{high_water, gap_start, gap_end, last_backfilled}` in a new `DeviceState` model. `pull_observations` head-fetches `[max(high_water, now − max_data_age_hours), now]` for every device every run, then triggers `backfill_observations` (an `InternalActionConfiguration` action, never shown in the portal) when gaps exist and the Redis lease is free. Anything older than `max_data_age_hours` that was never fetched is **dropped permanently** with a WARNING — bounded staleness is the deliberate, approved trade-off.

**Tech Stack:** Python 3.11, pydantic v1, httpx, stamina, redis.asyncio, pytest + pytest-asyncio + pytest-mock. Suite runs with `./venv/bin/python -m pytest app -q`.

## Global Constraints

- TDD strictly: failing test first, then minimal implementation. Flip-verify assertions that pin critical lines (change the production line, watch the test fail, restore byte-identical).
- Test suite must stay **< 2 s**: patch `RETRY_WAIT_INITIAL` / `RETRY_WAIT_JITTER` / `RETRY_WAIT_MAX` to 0 in any test that exercises retries. Never `sleep`.
- Health/alerting keys on ERROR-count only (cdip `calculate_integration_status`, ≥3 ERROR logs / 60 min → UNHEALTHY). ERROR = "someone can and should act": login refused, `get_devices` failure, delivery failure, zero-progress. WARNING = per-device transient fetch failures, deferrals (deadline/breaker), dropped stale ranges.
- Code constants, NOT config fields: `RETRY_ATTEMPTS = 2`, `DEADLINE_FRACTION = 0.8`, `BREAKER_THRESHOLD = 3`, `BACKFILL_MAX_WINDOWS_PER_DEVICE = 2`, `BACKFILL_WINDOW = timedelta(days=7)`.
- New config field: `max_data_age_hours`, default 12, ge=1, le=12, `ui:widget: range`. `ui:order` must list every property including hidden `run_on_schedule`.
- State keys: device state stays under `(integration_id, "pull_observations", device_id)` — shared by both actions. Backfill lease under `(integration_id, "backfill_observations", "lease")` via the existing atomic `IntegrationStateManager.set_if_absent` (NX + TTL = `settings.MAX_ACTION_EXECUTION_TIME`).
- `Optional[str]`, not `str | None` (pydantic v1 codebase style). All datetimes tz-aware UTC.
- Commit after every green task. Branch: `feat/GUNDI-5602-head-pass-backfill` off `origin/main`.

---

### Task 1: Branch + fold pending working-tree edits

The working tree already carries two reviewed edits (restored from stash): the `needs_attention` extra on the delivery-error `logger.exception` in `app/actions/handlers.py`, and `pytest.ini` `testpaths = app`. The spec doc is untracked. Commit all three on the new branch.

**Files:**
- Modify: (already modified) `app/actions/handlers.py`, `pytest.ini`
- Create: (already on disk, untracked) `docs/superpowers/specs/2026-08-13-lotek-head-pass-design.md`, this plan file

**Interfaces:**
- Produces: branch `feat/GUNDI-5602-head-pass-backfill` that every later task commits to.

- [ ] **Step 1: Create the branch**

```bash
git fetch origin && git checkout -b feat/GUNDI-5602-head-pass-backfill origin/main
```

(The dirty edits carry over — they don't conflict with `origin/main`.)

- [ ] **Step 2: Run the full suite to establish a green baseline**

Run: `./venv/bin/python -m pytest app -q`
Expected: all pass, < 2 s. If `testpaths = app` surfaces previously-uncollected failing tests, STOP and report — do not fix unrelated failures silently.

- [ ] **Step 3: Commit in two commits**

```bash
git add pytest.ini && git commit -m "fix: collect all tests under app/, not just app/actions/test"
git add app/actions/handlers.py docs/ && git commit -m "fix: restore needs_attention extra on delivery errors; add GUNDI-5602 spec + plan"
```

---

### Task 2: `get_devices` error message uses `describe_exception`

`action_pull_observations` line ~157 interpolates `{e}` directly; httpx timeout exceptions stringify empty, rendering `Exception: ` in the activity log.

**Files:**
- Modify: `app/actions/handlers.py:157`
- Test: `app/actions/tests/test_handlers.py`

**Interfaces:**
- Consumes: existing `describe_exception(exc)` in handlers.py.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_get_devices_failure_logs_exception_type_when_message_is_empty(
    mocker, lotek_integration, pull_config, mock_redis
):
    # httpx timeouts stringify to "" — the activity log must name the type,
    # not render a bare "Exception: ".
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_MAX", 0)
    mocker.patch(
        "app.actions.client.get_devices",
        new=AsyncMock(side_effect=httpx.ReadTimeout("")),
    )
    mock_log = mocker.patch("app.actions.handlers.log_action_activity", new=AsyncMock())
    with pytest.raises(httpx.ReadTimeout):
        await action_pull_observations(lotek_integration, pull_config)
    title = mock_log.call_args.kwargs["title"]
    assert "ReadTimeout" in title
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest app/actions/tests/test_handlers.py::test_get_devices_failure_logs_exception_type_when_message_is_empty -q`
Expected: FAIL — title ends with `Exception: ` (empty).

- [ ] **Step 3: Fix the message**

In `action_pull_observations`, change:

```python
        message = f"Error fetching devices from Lotek. Integration ID: {integration.id} Exception: {e}"
```

to:

```python
        message = f"Error fetching devices from Lotek. Integration ID: {integration.id} Exception: {describe_exception(e)}"
```

- [ ] **Step 4: Run the full suite**

Run: `./venv/bin/python -m pytest app -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/actions/handlers.py app/actions/tests/test_handlers.py
git commit -m "fix: name the exception type in get_devices failure logs"
```

---

### Task 3: `RETRY_ATTEMPTS` 3 → 2

Retry×budget amplification is a root cause of the 9-minute timeouts. One retry (2 attempts) still recovers token expiry and single timeouts.

**Files:**
- Modify: `app/actions/handlers.py:31`
- Test: `app/actions/tests/test_handlers.py`

**Interfaces:**
- Produces: `RETRY_ATTEMPTS == 2` — later tasks' retry tests assume 2 attempts.

- [ ] **Step 1: Write the failing pin test**

```python
def test_retry_attempts_is_two():
    # 3 attempts × 9 windows amplified Lotek's slowness into our own 9-min
    # timeouts (GUNDI-5602). One retry still recovers token expiry.
    assert RETRY_ATTEMPTS == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest app/actions/tests/test_handlers.py::test_retry_attempts_is_two -q`
Expected: FAIL — currently 3.

- [ ] **Step 3: Change the constant**

```python
RETRY_ATTEMPTS = 2
```

- [ ] **Step 4: Run the full suite; fix retry-count assertions**

Run: `./venv/bin/python -m pytest app -q`
`test_action_pull_observations_retries_transient_timeout` and `test_action_pull_observations_isolates_persistent_401_on_one_device` import `RETRY_ATTEMPTS`, so counts should self-adjust — verify they still assert something meaningful (a side_effect list sized `RETRY_ATTEMPTS` with success last must still pass).

- [ ] **Step 5: Commit**

```bash
git add app/actions/handlers.py app/actions/tests/test_handlers.py
git commit -m "fix: reduce position-fetch retries to 2 attempts (GUNDI-5602 load relief)"
```

---

### Task 4: `DeviceState` model with legacy-cursor migration

New module holding the per-device state schema. Replaces `client.IntegrationState` for handler use.

**Files:**
- Create: `app/actions/device_state.py`
- Test: `app/actions/tests/test_device_state.py`

**Interfaces:**
- Produces: `DeviceState(high_water: datetime, gap_start: Optional[datetime], gap_end: Optional[datetime], last_backfilled: Optional[datetime])`, property `has_gap -> bool`. `DeviceState.parse_obj({"updated_at": ...})` migrates the legacy cursor to `high_water` with no gap. All later tasks import `from app.actions.device_state import DeviceState`.

- [ ] **Step 1: Write the failing tests**

```python
import pytest
import pydantic
from datetime import datetime, timezone

from app.actions.device_state import DeviceState


def test_legacy_updated_at_cursor_parses_as_high_water_with_no_gap():
    # Pre-5602 state stored {"updated_at": ...}; deployed integrations must
    # carry their cursor over without opening a gap.
    state = DeviceState.parse_obj({"updated_at": "2026-08-10T00:00:00+00:00"})
    assert state.high_water == datetime(2026, 8, 10, tzinfo=timezone.utc)
    assert state.gap_start is None and state.gap_end is None
    assert not state.has_gap


def test_naive_datetimes_are_assumed_utc():
    state = DeviceState.parse_obj({"high_water": "2026-08-10T00:00:00"})
    assert state.high_water.tzinfo is not None
    assert state.high_water == datetime(2026, 8, 10, tzinfo=timezone.utc)


def test_has_gap_true_only_for_a_nonempty_range():
    base = {"high_water": "2026-08-13T00:00:00+00:00"}
    open_gap = DeviceState.parse_obj(
        {**base, "gap_start": "2026-08-01T00:00:00+00:00", "gap_end": "2026-08-05T00:00:00+00:00"}
    )
    assert open_gap.has_gap
    empty_gap = DeviceState.parse_obj(
        {**base, "gap_start": "2026-08-05T00:00:00+00:00", "gap_end": "2026-08-05T00:00:00+00:00"}
    )
    assert not empty_gap.has_gap


def test_missing_cursor_is_a_validation_error():
    # No high_water and no legacy updated_at → caller must treat as first run.
    with pytest.raises(pydantic.ValidationError):
        DeviceState.parse_obj({"error": "whatever"})


def test_round_trips_through_state_manager_json():
    # state_manager serializes with json.dumps(default=str); the model must
    # re-parse its own dict() output after that trip.
    import json
    state = DeviceState.parse_obj(
        {
            "high_water": "2026-08-13T00:00:00+00:00",
            "gap_start": "2026-08-01T00:00:00+00:00",
            "gap_end": "2026-08-06T00:00:00+00:00",
            "last_backfilled": "2026-08-12T00:00:00+00:00",
        }
    )
    reparsed = DeviceState.parse_obj(json.loads(json.dumps(state.dict(), default=str)))
    assert reparsed == state
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/python -m pytest app/actions/tests/test_device_state.py -q`
Expected: FAIL — `ModuleNotFoundError: app.actions.device_state`.

- [ ] **Step 3: Implement the model**

Create `app/actions/device_state.py`:

```python
import pydantic

from datetime import datetime, timezone
from typing import Optional


def _ensure_utc(v):
    if v is None:
        return v
    if not v.tzinfo:
        return v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc)


class DeviceState(pydantic.BaseModel):
    """Per-device sync state (GUNDI-5602 head-pass design).

    high_water is the newest upload-time already synced. [gap_start, gap_end)
    is the single unfetched historical range — created once on a device's
    first run, it only ever shrinks and is never extended. last_backfilled
    orders backfill fairness (least-recently-backfilled first).
    """
    high_water: datetime
    gap_start: Optional[datetime] = None
    gap_end: Optional[datetime] = None
    last_backfilled: Optional[datetime] = None

    @pydantic.root_validator(pre=True)
    def _migrate_legacy_cursor(cls, values):
        # Pre-5602 state stored the cursor as updated_at; parse it as
        # high_water so deployed integrations carry over without a gap.
        if values.get("high_water") is None and values.get("updated_at") is not None:
            values = dict(values)
            values["high_water"] = values["updated_at"]
        return values

    _tz = pydantic.validator(
        "high_water", "gap_start", "gap_end", "last_backfilled", allow_reuse=True
    )(_ensure_utc)

    @property
    def has_gap(self) -> bool:
        return (
            self.gap_start is not None
            and self.gap_end is not None
            and self.gap_start < self.gap_end
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest app/actions/tests/test_device_state.py -q`
Expected: PASS.

- [ ] **Step 5: Flip-verify the migration line**

Temporarily change `values["high_water"] = values["updated_at"]` to `values["high_water"] = None` → `test_legacy_updated_at_cursor_parses_as_high_water_with_no_gap` must FAIL. Restore byte-identical, re-run, PASS.

- [ ] **Step 6: Commit**

```bash
git add app/actions/device_state.py app/actions/tests/test_device_state.py
git commit -m "feat: DeviceState model with high_water/gap fields and legacy cursor migration"
```

---

### Task 5: Config — `max_data_age_hours` slider + `BackfillObservationsConfig`

**Files:**
- Modify: `app/actions/configurations.py`
- Test: `app/actions/tests/test_handlers.py` (config tests live alongside; a new `test_configurations.py` is fine too — use `app/actions/tests/test_configurations.py`)

**Interfaces:**
- Produces: `PullObservationsConfig.max_data_age_hours: int` (default 12, ge=1, le=12); `BackfillObservationsConfig(InternalActionConfiguration)` with no fields. Handlers import `BackfillObservationsConfig` from `app.actions.configurations`.

- [ ] **Step 1: Write the failing tests** (`app/actions/tests/test_configurations.py`)

```python
import pydantic
import pytest

from app.actions.configurations import BackfillObservationsConfig, PullObservationsConfig
from app.actions.core import InternalActionConfiguration


def test_max_data_age_hours_defaults_to_12_and_is_bounded_1_to_12():
    assert PullObservationsConfig().max_data_age_hours == 12
    with pytest.raises(pydantic.ValidationError):
        PullObservationsConfig(max_data_age_hours=0)
    with pytest.raises(pydantic.ValidationError):
        PullObservationsConfig(max_data_age_hours=13)


def test_max_data_age_hours_renders_as_range_slider():
    ui = PullObservationsConfig.ui_schema()
    assert ui["max_data_age_hours"]["ui:widget"] == "range"


def test_ui_order_lists_every_property_including_hidden_run_on_schedule():
    # rjsf + ajv strict mode fails silently when ui:order misses a property.
    ui = PullObservationsConfig.ui_schema()
    assert set(ui["ui:order"]) == set(PullObservationsConfig.schema()["properties"].keys())


def test_backfill_config_is_internal_and_fieldless():
    # InternalActionConfiguration subclasses are skipped at registration —
    # backfill must never appear in the portal.
    assert issubclass(BackfillObservationsConfig, InternalActionConfiguration)
    assert BackfillObservationsConfig.schema().get("properties", {}) == {}
```

Note: verify the exact `ui_schema()` output shape against `UISchemaModelMixin` in `app/services/utils.py` when the first test run fails — the assertion keys above assume rjsf conventions (`"ui:order"`, per-field `"ui:widget"`); adjust the *test's key access* (not its intent) if the mixin nests differently. The existing `run_on_schedule` hidden widget is a working reference.

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/python -m pytest app/actions/tests/test_configurations.py -q`
Expected: FAIL — `ImportError: BackfillObservationsConfig` / missing field.

- [ ] **Step 3: Implement**

In `app/actions/configurations.py`, extend the import from `.core`:

```python
from .core import (
    AuthActionConfiguration,
    ExecutableActionMixin,
    InternalActionConfiguration,
    PullActionConfiguration,
)
```

Add to `PullObservationsConfig` after `default_lookback_days` (and narrow that field's description per the spec):

```python
    default_lookback_days: int = pydantic.Field(
        7,
        ge=1,
        le=60,
        title="Default lookback (days)",
        description="How many days of historic data to import when a device is first seen.",
    )
    max_data_age_hours: int = FieldWithUIOptions(
        12,
        ge=1,
        le=12,
        title="Max data age (hours)",
        description=(
            "Freshness bound: every run fetches at most this many hours back. "
            "Positions uploaded longer ago than this that could not be fetched "
            "are skipped permanently."
        ),
        ui_options=UIOptions(widget="range"),
    )
```

Update `ui_global_options`:

```python
    ui_global_options: GlobalUISchemaOptions = GlobalUISchemaOptions(
        order=[
            "default_lookback_days",
            "max_data_age_hours",
            "max_pdop",
            "run_on_schedule",
        ],
    )
```

Add at the bottom:

```python
class BackfillObservationsConfig(InternalActionConfiguration):
    """Internal-only: triggered by pull_observations, never configured in the portal."""
    pass
```

- [ ] **Step 4: Run the full suite**

Run: `./venv/bin/python -m pytest app -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/actions/configurations.py app/actions/tests/test_configurations.py
git commit -m "feat: max_data_age_hours slider config + internal backfill config (GUNDI-5602)"
```

---

### Task 6: Head-pass rewrite of `pull_observations`

The big one. Replaces the oldest-first chunk walk in `_pull_device_observations` with a single head fetch per device, first-run gap creation, permanent stale-drop, and the WARNING demotion for per-device transient fetch failures. Delivery/checkpoint failures stay ERROR. Result gains `devices_deferred: []` (populated by Tasks 8–9).

**Files:**
- Modify: `app/actions/handlers.py` (replace `_pull_device_observations` with `_load_device_state` + `_head_pass_device`; rewrite the loop body of `action_pull_observations`)
- Test: `app/actions/tests/test_handlers.py`

**Interfaces:**
- Consumes: `DeviceState` (Task 4), `max_data_age_hours` (Task 5).
- Produces:
  - `async def _load_device_state(integration_id: str, device_id: str, present_time: datetime, action_config) -> DeviceState`
  - `async def _head_pass_device(device, integration, auth, action_config, present_time) -> tuple[int, bool, Optional[DeviceState]]` — `(observations_sent, device_failed, state)`; raises only `LotekUnauthorizedException` (integration-wide); records nothing about breakers yet (Task 8 adds the `guards` parameter, Task 9 uses it for the breaker).
  - `action_pull_observations` returns `{'observations_extracted': int, 'devices_failed': list, 'devices_deferred': list}`.

- [ ] **Step 1: Write the failing tests**

Add to `test_handlers.py` (shared setup mirrors existing tests; `freshness floor` = `now − max_data_age_hours`):

```python
def _setup_pull_mocks(mocker, mock_redis, devices, positions=None, saved_state=None):
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=devices))
    get_positions = mocker.patch(
        "app.actions.client.get_positions", new=AsyncMock(return_value=positions or [])
    )
    get_state = mocker.patch(
        "app.services.state.IntegrationStateManager.get_state",
        new=AsyncMock(return_value=saved_state or {}),
    )
    set_state = mocker.patch(
        "app.services.state.IntegrationStateManager.set_state", new=AsyncMock(return_value=None)
    )
    mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    log = mocker.patch("app.actions.handlers.log_action_activity", new=AsyncMock())
    return get_positions, get_state, set_state, log


@pytest.mark.asyncio
async def test_head_pass_fetches_single_max_age_window_on_first_run(
    mocker, lotek_integration, pull_config, mock_redis
):
    # First run: ONE request per device covering [now - max_data_age_hours, now]
    # — not a chunked walk over the whole lookback.
    get_positions, _, _, _ = _setup_pull_mocks(mocker, mock_redis, _devices("1"))
    await action_pull_observations(lotek_integration, pull_config)
    assert get_positions.await_count == 1
    start, end = get_positions.call_args.args[3], get_positions.call_args.args[4]
    assert abs((end - start) - timedelta(hours=pull_config.max_data_age_hours)) < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_first_run_opens_gap_from_lookback_to_freshness_floor(
    mocker, lotek_integration, pull_config, mock_redis
):
    _, _, set_state, _ = _setup_pull_mocks(mocker, mock_redis, _devices("1"))
    await action_pull_observations(lotek_integration, pull_config)
    saved = set_state.call_args.args[2]
    gap_span = saved["gap_end"] - saved["gap_start"]
    expected = timedelta(days=pull_config.default_lookback_days) - timedelta(
        hours=pull_config.max_data_age_hours
    )
    assert abs(gap_span - expected) < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_no_gap_opened_when_lookback_fits_inside_max_age(mocker):
    # Portal bounds (lookback >= 1 day > max_age <= 12h) make this unreachable
    # via valid config, but the guard in _load_device_state must hold anyway —
    # pin it at the unit level with construct() to bypass validation.
    from app.actions.handlers import _load_device_state
    from app.actions.configurations import PullObservationsConfig
    mocker.patch(
        "app.services.state.IntegrationStateManager.get_state",
        new=AsyncMock(return_value={}),
    )
    config = PullObservationsConfig.construct(
        default_lookback_days=1, max_data_age_hours=48, max_pdop=None
    )
    state = await _load_device_state(
        "some-integration-id", "1", datetime.now(timezone.utc), config
    )
    assert not state.has_gap
    assert state.gap_start is None and state.gap_end is None


@pytest.mark.asyncio
async def test_steady_state_advances_high_water_and_keeps_gap_closed(
    mocker, lotek_integration, pull_config, mock_redis
):
    # A device whose cursor is fresh (within max_age) head-fetches from its
    # cursor and neither opens a gap nor drops anything.
    recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    get_positions, _, set_state, log = _setup_pull_mocks(
        mocker, mock_redis, _devices("1"), saved_state={"high_water": recent}
    )
    result = await action_pull_observations(lotek_integration, pull_config)
    start = get_positions.call_args.args[3]
    assert abs((datetime.now(timezone.utc) - start) - timedelta(hours=2)) < timedelta(minutes=1)
    saved = set_state.call_args.args[2]
    assert saved.get("gap_start") is None
    assert result["devices_deferred"] == []
    warning_titles = [
        c.kwargs["title"] for c in log.await_args_list if c.kwargs["level"] == LogLevel.WARNING
    ]
    assert not any("stale" in t.lower() for t in warning_titles)


@pytest.mark.asyncio
async def test_stale_span_is_dropped_with_warning_and_not_added_to_gap(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Bounded staleness (agreed design decision): a cursor further back than max_age
    # means that span is dropped permanently — WARNING with the range, gap unchanged.
    stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    get_positions, _, set_state, log = _setup_pull_mocks(
        mocker, mock_redis, _devices("1"), saved_state={"high_water": stale}
    )
    await action_pull_observations(lotek_integration, pull_config)
    start = get_positions.call_args.args[3]
    assert abs(
        (datetime.now(timezone.utc) - start) - timedelta(hours=pull_config.max_data_age_hours)
    ) < timedelta(minutes=1)
    saved = set_state.call_args.args[2]
    assert saved.get("gap_start") is None  # NOT added to the gap
    drop_warnings = [
        c for c in log.await_args_list
        if c.kwargs["level"] == LogLevel.WARNING and "Dropping stale range" in c.kwargs["title"]
    ]
    assert len(drop_warnings) == 1
    assert "device 1" in drop_warnings[0].kwargs["title"]


@pytest.mark.asyncio
async def test_legacy_updated_at_state_migrates_to_high_water(
    mocker, lotek_integration, pull_config, mock_redis
):
    recent = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    get_positions, _, set_state, _ = _setup_pull_mocks(
        mocker, mock_redis, _devices("1"), saved_state={"updated_at": recent}
    )
    await action_pull_observations(lotek_integration, pull_config)
    start = get_positions.call_args.args[3]
    assert abs((datetime.now(timezone.utc) - start) - timedelta(hours=3)) < timedelta(minutes=1)
    saved = set_state.call_args.args[2]
    assert "high_water" in saved and saved.get("gap_start") is None


@pytest.mark.asyncio
async def test_transient_fetch_failure_logs_warning_not_error(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Health/alerting keys on ERROR count; recurring per-device timeouts must
    # not mark the connection unhealthy. devices_failed already tracks them.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_MAX", 0)
    get_positions, _, set_state, log = _setup_pull_mocks(
        mocker, mock_redis, _devices("1", "2")
    )
    get_positions.side_effect = [httpx.ReadTimeout("")] * RETRY_ATTEMPTS + [[]]
    result = await action_pull_observations(lotek_integration, pull_config)
    assert result["devices_failed"] == ["1"]
    device_1_logs = [
        c for c in log.await_args_list if "Device: 1" in c.kwargs.get("title", "")
    ]
    assert device_1_logs and all(c.kwargs["level"] == LogLevel.WARNING for c in device_1_logs)


@pytest.mark.asyncio
async def test_failed_head_fetch_does_not_advance_high_water(
    mocker, lotek_integration, pull_config, mock_redis
):
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_MAX", 0)
    recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    get_positions, _, set_state, _ = _setup_pull_mocks(
        mocker, mock_redis, _devices("1"), saved_state={"high_water": recent}
    )
    get_positions.side_effect = httpx.ReadTimeout("")
    with pytest.raises(LotekException):
        # single device, nothing serviced → zero-progress raise (Task 7 keeps this
        # as the all-failed raise until then: adapt to whichever exists when writing)
        await action_pull_observations(lotek_integration, pull_config)
    set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivery_failure_stays_error_and_does_not_advance_high_water(
    mocker, lotek_integration, lotek_position, pull_config, mock_redis
):
    get_positions, _, set_state, log = _setup_pull_mocks(
        mocker, mock_redis, _devices("1", "2"), positions=[lotek_position]
    )
    send = mocker.patch(
        "app.actions.handlers.gundi_tools.send_observations_to_gundi",
        new=AsyncMock(side_effect=[Exception("boom"), None]),
    )
    result = await action_pull_observations(lotek_integration, pull_config)
    assert result["devices_failed"] == ["1"]
    error_logs = [
        c for c in log.await_args_list
        if c.kwargs["level"] == LogLevel.ERROR and "delivering" in c.kwargs["title"].lower()
    ]
    assert len(error_logs) == 1
    # only device 2's checkpoint was written
    saved_devices = [c.args[3] for c in set_state.await_args_list]
    assert saved_devices == ["2"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/python -m pytest app/actions/tests/test_handlers.py -q -k "head_pass or first_run or steady_state or stale_span or legacy_updated or transient_fetch or delivery_failure_stays"`
Expected: FAIL (chunked walk, missing keys, ERROR levels).

- [ ] **Step 3: Implement the rewrite**

In `app/actions/handlers.py`:

Imports and constants (top of file):

```python
from app.actions.device_state import DeviceState
from app.services.action_scheduler import trigger_action
```

```python
RETRY_ATTEMPTS = 2
...
MAX_DEVICES_IN_SUMMARY = 20
# Safety-rail constants (GUNDI-5602): load-protection mechanics, deliberately
# code constants rather than operator knobs.
DEADLINE_FRACTION = 0.8
BREAKER_THRESHOLD = 3
BACKFILL_MAX_WINDOWS_PER_DEVICE = 2
BACKFILL_WINDOW = timedelta(days=7)
BACKFILL_LEASE_SOURCE = "lease"
```

New state loader (replaces the `client.IntegrationState` block):

```python
async def _load_device_state(integration_id, device_id, present_time, action_config):
    saved = await state_manager.get_state(integration_id, "pull_observations", device_id)
    if saved:
        try:
            return DeviceState.parse_obj(saved)
        except pydantic.ValidationError as e:
            logger.debug(f"Failed to parse saved state for device {device_id}, starting fresh. Error: {e}")
    # First run: the head pass starts at the freshness floor; everything older,
    # back to the configured lookback, becomes the device's one and only gap —
    # the deliberate historical import. It only ever shrinks from here.
    head_start = present_time - timedelta(hours=action_config.max_data_age_hours)
    gap_start = present_time - timedelta(days=action_config.default_lookback_days)
    if gap_start < head_start:
        return DeviceState(high_water=head_start, gap_start=gap_start, gap_end=head_start)
    return DeviceState(high_water=head_start)
```

Head-pass worker (replaces `_pull_device_observations`; keep the no-positions info WARNING, the `needs_attention` delivery-error block, and the checkpoint-error block from the old function — they move over nearly verbatim):

```python
async def _head_pass_device(device, integration, auth, action_config, present_time):
    """Fetch, deliver and checkpoint one device's freshest window.

    Returns (observations_sent, device_failed, state). Raises only for
    integration-wide problems; per-device problems are reported through the
    returned flag so the caller can keep going. Fetch-phase failures are
    WARNINGs (transient while Lotek is slow; devices_failed tracks them);
    delivery/checkpoint failures stay ERRORs.
    """
    integration_id = str(integration.id)
    freshness_floor = present_time - timedelta(hours=action_config.max_data_age_hours)
    state = await _load_device_state(integration_id, device.nDeviceID, present_time, action_config)

    if state.high_water < freshness_floor:
        # Bounded staleness (GUNDI-5602, deliberate): anything the cursor still
        # owed beyond max_data_age_hours is dropped permanently — never added
        # to the gap — so catch-up cost cannot compound.
        message = (
            f"Dropping stale range [{state.high_water.isoformat()}, {freshness_floor.isoformat()}] "
            f"for device {device.nDeviceID}: older than max_data_age_hours="
            f"{action_config.max_data_age_hours}. Integration ID: {integration_id}"
        )
        logger.warning(message)
        await log_action_activity(
            integration_id=integration_id,
            action_id="pull_observations",
            title=message,
            level=LogLevel.WARNING,
        )

    lower_date = max(state.high_water, freshness_floor)
    try:
        async for attempt in stamina.retry_context(
            on=RETRYABLE_ERRORS, attempts=RETRY_ATTEMPTS,
            wait_initial=RETRY_WAIT_INITIAL, wait_jitter=RETRY_WAIT_JITTER, wait_max=RETRY_WAIT_MAX,
        ):
            with attempt:
                positions = await client.get_positions(
                    device.nDeviceID, auth, integration, lower_date, present_time, True
                )
        logger.info(
            f"Extracted {len(positions)} obs from Lotek for device: {device.nDeviceID} "
            f"between {lower_date} and {present_time}."
        )
        cdip_positions = filter_and_transform_positions(positions, integration, action_config)
    except LotekUnauthorizedException:
        # Credentials are an integration-wide problem: every remaining device
        # would fail the same way, so fail fast instead of N identical errors.
        raise
    except Exception as e:
        # WARNING, not ERROR: these recur daily while Lotek is slow, health
        # keys on ERROR count alone, and devices_failed already tracks them.
        message = (
            f"Error fetching positions from Lotek. Device: {device.nDeviceID}. "
            f"Dates: [{lower_date},{present_time}]. Integration ID: {integration_id} "
            f"Exception: {describe_exception(e)}"
        )
        logger.warning(message, exc_info=True)
        await log_action_activity(
            integration_id=integration_id,
            action_id="pull_observations",
            title=message,
            level=LogLevel.WARNING,
        )
        return 0, True, state

    # ... no-positions info WARNING block: unchanged from the old function ...

    observations_sent = 0
    # ... send loop with the ERROR + needs_attention handler: unchanged, but on
    #     failure `return observations_sent, True, state` (high_water untouched;
    #     the head window is re-fetched next run — re-sends are tolerated,
    #     silent skips are not) ...

    # Advance the cursor to the queried upper bound (upload time). Must advance
    # even when the device returned no positions.
    state.high_water = present_time
    # ... checkpoint save with the ERROR handler: unchanged, but save
    #     `state.dict()` instead of {"updated_at": ...};
    #     on failure `return observations_sent, True, state` ...
    return observations_sent, False, state
```

Rewritten action loop (structure of `action_pull_observations` after `get_devices`):

```python
    present_time = datetime.now(tz=timezone.utc)
    observations_extracted = 0
    failed_devices = []
    deferred_devices = []
    serviced_devices = 0
    any_open_gap = False
    for device in device_list:
        try:
            sent, device_failed, state = await _head_pass_device(
                device, integration, auth, action_config, present_time
            )
        except LotekUnauthorizedException:
            raise
        except Exception as e:
            message = (
                f"Failed to process device {device.nDeviceID} for integration "
                f"{integration.id}: {describe_exception(e)}"
            )
            logger.exception(message)
            await log_action_activity(
                integration_id=str(integration.id),
                action_id="pull_observations",
                title=message,
                level=LogLevel.ERROR,
            )
            failed_devices.append(device.nDeviceID)
            continue
        observations_extracted += sent
        if state is not None and state.has_gap:
            any_open_gap = True
        if device_failed:
            failed_devices.append(device.nDeviceID)
        else:
            serviced_devices += 1
```

Keep the failed-device summary WARNING block unchanged. Keep the all-failed raise for now (Task 7 generalizes it). Then before the return:

```python
    if any_open_gap:
        try:
            lease = await state_manager.get_state(
                str(integration.id), "backfill_observations", BACKFILL_LEASE_SOURCE
            )
            if not lease:
                await trigger_action(str(integration.id), "backfill_observations")
        except Exception as e:
            # The head pass succeeded; a failed trigger must not fail the run.
            logger.warning(
                f"Could not trigger backfill for integration {integration.id}: {describe_exception(e)}"
            )

    return {
        'observations_extracted': observations_extracted,
        'devices_failed': failed_devices,
        'devices_deferred': deferred_devices,
    }
```

- [ ] **Step 4: Update existing tests that pinned the old chunk-walk semantics**

These existing tests change meaning — update, don't delete their intent:
- `test_lookback_days_config_sets_first_run_window` → first run makes exactly **1** head call spanning `max_data_age_hours`, and opens the gap (covered by new tests; rewrite this one to assert the gap span honors `default_lookback_days=30`).
- `test_action_pull_observations_keeps_successful_chunks_when_later_chunk_fails` → chunk-walk is gone from the head pass; DELETE (the equivalent per-window checkpoint behavior is pinned in backfill, Task 10).
- `test_action_pull_observations_does_not_advance_state_for_failed_device` → keep intent; state shape is now `DeviceState` and failure level is WARNING.
- Every `result == {...}` assertion gains `'devices_deferred': []`.
- Error-level assertions on per-device fetch failures flip ERROR → WARNING (`test_action_pull_observations_continues_after_one_device_fails`, `..._continues_after_lotek_error_status`, `..._continues_after_malformed_device_data`, `..._isolates_persistent_401_on_one_device`, `..._logs_exception_type_when_message_is_empty`, `..._retries_transient_timeout`).
- `test_action_pull_observations_aborts_when_login_is_rejected`, `..._aborts_on_auth_failure`, `..._does_not_retry_rejected_login_at_get_devices`, auth tests: unchanged.

- [ ] **Step 5: Run the full suite**

Run: `./venv/bin/python -m pytest app -q`
Expected: PASS, < 2 s.

- [ ] **Step 6: Flip-verify two critical lines**

1. Change `state.high_water = present_time` to `state.high_water = state.high_water` → steady-state test must fail. Restore.
2. In the stale-drop branch, add `state.gap_start = state.high_water; state.gap_end = freshness_floor` (growing the gap) → `test_stale_span_is_dropped_with_warning_and_not_added_to_gap` must fail. Restore byte-identical.

- [ ] **Step 7: Commit**

```bash
git add app/actions/handlers.py app/actions/tests/test_handlers.py
git commit -m "feat: newest-first head pass with bounded staleness (GUNDI-5602)"
```

---

### Task 7: Zero-progress guard

Generalize the all-failed raise: a run where **no device was serviced and nothing was delivered** is systemic degradation and must ERROR, including when everything was deferred by the rails (Tasks 8–9).

**Files:**
- Modify: `app/actions/handlers.py`
- Test: `app/actions/tests/test_handlers.py`

**Interfaces:**
- Produces: the raise fires on `device_list and serviced_devices == 0 and observations_extracted == 0`. Backfill (Task 10) reuses the same predicate shape inline.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_zero_progress_run_raises_even_when_devices_were_only_deferred(
    mocker, lotek_integration, pull_config, mock_redis
):
    # A run that services nothing is systemic degradation — it must alert
    # (ERROR/raise), not warn forever. Deferring every device counts.
    _setup_pull_mocks(mocker, mock_redis, _devices("1", "2"))
    mocker.patch("app.actions.handlers._deadline_exceeded", return_value=True)
    with pytest.raises(LotekException, match="No devices could be serviced"):
        await action_pull_observations(lotek_integration, pull_config)
```

(This test needs Task 8's `_deadline_exceeded` to exist; write it as `@pytest.mark.skip(reason="needs deadline rail")` now and un-skip in Task 8, OR reorder Steps so the message change is tested via the all-failed path:)

```python
@pytest.mark.asyncio
async def test_all_failed_run_raises_zero_progress(
    mocker, lotek_integration, pull_config, mock_redis
):
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_MAX", 0)
    get_positions, _, _, _ = _setup_pull_mocks(mocker, mock_redis, _devices("1", "2"))
    get_positions.side_effect = httpx.ReadTimeout("")
    with pytest.raises(LotekException, match="No devices could be serviced"):
        await action_pull_observations(lotek_integration, pull_config)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest app/actions/tests/test_handlers.py::test_all_failed_run_raises_zero_progress -q`
Expected: FAIL — old message is "All N device(s) failed…".

- [ ] **Step 3: Replace the all-failed raise**

```python
    if device_list and serviced_devices == 0 and observations_extracted == 0:
        # Zero progress: nothing serviced and nothing delivered — whether every
        # device failed or the rails deferred them all, this is systemic
        # degradation and must alert rather than publish action_complete. A
        # device that delivered part of its window before failing still counts
        # as progress, so partial runs stay warnings.
        raise LotekException(
            message=(
                f"No devices could be serviced for integration {integration.id}: "
                f"{len(failed_devices)} failed, {len(deferred_devices)} deferred of "
                f"{len(device_list)}. See the per-device errors in this action's activity log."
            )
        )
```

- [ ] **Step 4: Update `test_action_pull_observations_fails_when_every_device_fails` to the new message; run the full suite**

Run: `./venv/bin/python -m pytest app -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/actions/handlers.py app/actions/tests/test_handlers.py
git commit -m "feat: zero-progress guard — raise when a run services no devices"
```

---

### Task 8: Deadline rail

Stop starting new device work past `DEADLINE_FRACTION` (80%) of `MAX_ACTION_EXECUTION_TIME`; defer the tail with a WARNING and `devices_deferred`. Also gate retry attempts on the deadline.

**Files:**
- Modify: `app/actions/handlers.py`
- Test: `app/actions/tests/test_handlers.py`

**Interfaces:**
- Produces:
  - `def _deadline_exceeded(run_started_at: datetime) -> bool`
  - `def _retry_attempts(run_started_at: datetime) -> int` — 1 past deadline, else `RETRY_ATTEMPTS`
  - `class RunGuards` with `run_started_at`, `should_stop() -> Optional[str]` (returns `"deadline"` / `"circuit breaker"` / None) and `record(transport_failure: bool)`. Task 9 adds the breaker arm; Task 10 reuses the whole class.
  - `async def _log_deferral(integration, action_id, reason, deferred_ids)` — WARNING activity log naming the reason and count.
  - `_head_pass_device` gains a `guards` parameter (used for `_retry_attempts` and failure recording).

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_deadline_defers_remaining_devices_with_warning(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Past ~80% of the action budget we stop STARTING device work and exit in
    # control — never via the asyncio.wait_for guillotine. Call order per
    # device: should_stop() then _retry_attempts(), hence the side_effect
    # sequence [False(stop d1), False(retry d1), True(stop d2)].
    _, _, _, log = _setup_pull_mocks(mocker, mock_redis, _devices("1", "2", "3"))
    mocker.patch(
        "app.actions.handlers._deadline_exceeded", side_effect=[False, False, True]
    )
    result = await action_pull_observations(lotek_integration, pull_config)
    assert result["devices_deferred"] == ["2", "3"]
    assert result["devices_failed"] == []
    deferral_logs = [
        c for c in log.await_args_list
        if c.kwargs["level"] == LogLevel.WARNING and "deadline" in c.kwargs["title"].lower()
    ]
    assert len(deferral_logs) == 1


def test_retry_attempts_drop_to_one_past_deadline(mocker):
    from app.actions.handlers import _retry_attempts, RETRY_ATTEMPTS
    mocker.patch("app.actions.handlers._deadline_exceeded", return_value=False)
    assert _retry_attempts(datetime.now(timezone.utc)) == RETRY_ATTEMPTS
    mocker.patch("app.actions.handlers._deadline_exceeded", return_value=True)
    assert _retry_attempts(datetime.now(timezone.utc)) == 1


def test_deadline_fraction_of_budget():
    # 540s budget → soft deadline ~432s. Pin the fraction.
    from app.actions.handlers import DEADLINE_FRACTION
    assert DEADLINE_FRACTION == 0.8
```

Also un-skip / add `test_zero_progress_run_raises_even_when_devices_were_only_deferred` from Task 7.

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/python -m pytest app/actions/tests/test_handlers.py -q -k "deadline or zero_progress"`
Expected: FAIL — names don't exist.

- [ ] **Step 3: Implement**

```python
def _deadline_exceeded(run_started_at):
    elapsed = (datetime.now(tz=timezone.utc) - run_started_at).total_seconds()
    return elapsed > DEADLINE_FRACTION * settings.MAX_ACTION_EXECUTION_TIME


def _retry_attempts(run_started_at):
    # Past the soft deadline, don't spend the remaining budget on retries.
    return 1 if _deadline_exceeded(run_started_at) else RETRY_ATTEMPTS


class RunGuards:
    """Per-run deadline + circuit-breaker state for a device loop."""

    def __init__(self, run_started_at):
        self.run_started_at = run_started_at
        self.consecutive_transport_failures = 0

    def should_stop(self):
        if _deadline_exceeded(self.run_started_at):
            return "deadline"
        return None

    def record(self, transport_failure: bool):
        if transport_failure:
            self.consecutive_transport_failures += 1
        else:
            self.consecutive_transport_failures = 0


async def _log_deferral(integration, action_id, reason, deferred_ids):
    listed = ', '.join(deferred_ids[:MAX_DEVICES_IN_SUMMARY])
    if len(deferred_ids) > MAX_DEVICES_IN_SUMMARY:
        listed += f" and {len(deferred_ids) - MAX_DEVICES_IN_SUMMARY} more"
    message = (
        f"Stopping early ({reason}) for integration {integration.id}: deferring "
        f"{len(deferred_ids)} device(s) to the next run: {listed}."
    )
    logger.warning(message)
    await log_action_activity(
        integration_id=str(integration.id),
        action_id=action_id,
        title=message,
        level=LogLevel.WARNING,
    )
```

Wire into `action_pull_observations`: capture `run_started = datetime.now(tz=timezone.utc)` **before** `get_devices` (the devices call spends budget too); `guards = RunGuards(run_started)`; loop becomes:

```python
    for i, device in enumerate(device_list):
        if reason := guards.should_stop():
            deferred_devices = [d.nDeviceID for d in device_list[i:]]
            await _log_deferral(integration, "pull_observations", reason, deferred_devices)
            break
        try:
            sent, device_failed, state = await _head_pass_device(
                device, integration, auth, action_config, present_time, guards
            )
        ...
```

In `_head_pass_device`, accept `guards` and use `attempts=_retry_attempts(guards.run_started_at)` in the position-fetch `retry_context`. In the outer per-device `except Exception` of the action loop, call `guards.record(transport_failure=False)`.

- [ ] **Step 4: Run the full suite**

Run: `./venv/bin/python -m pytest app -q`
Expected: PASS.

- [ ] **Step 5: Flip-verify**

Change `should_stop`'s deadline branch to `return None` → deadline test fails. Restore byte-identical.

- [ ] **Step 6: Commit**

```bash
git add app/actions/handlers.py app/actions/tests/test_handlers.py
git commit -m "feat: soft deadline defers device work before the action-budget guillotine"
```

---

### Task 9: Circuit breaker

K=3 **consecutive** transport-class failures (timeouts/transport) → assume Lotek-wide degradation, stop, defer the remainder.

**Files:**
- Modify: `app/actions/handlers.py`
- Test: `app/actions/tests/test_handlers.py`

**Interfaces:**
- Consumes: `RunGuards` (Task 8).
- Produces: `RunGuards.should_stop()` also returns `"circuit breaker"`; `_head_pass_device` calls `guards.record(transport_failure=True)` on `httpx.TransportError` fetch failures, `record(False)` on other fetch failures and on success.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_breaker_trips_after_three_consecutive_transport_failures(
    mocker, lotek_integration, pull_config, mock_redis
):
    # 3 consecutive timeouts = Lotek-wide degradation: stop early, defer the
    # rest (WARNING), instead of grinding every device into the same wall.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_MAX", 0)
    get_positions, _, _, log = _setup_pull_mocks(
        mocker, mock_redis, _devices("1", "2", "3", "4", "5")
    )
    # device 1 succeeds; 2,3,4 exhaust retries on timeouts; 5 must be deferred
    get_positions.side_effect = [[]] + [httpx.ReadTimeout("")] * (3 * RETRY_ATTEMPTS)
    result = await action_pull_observations(lotek_integration, pull_config)
    assert result["devices_failed"] == ["2", "3", "4"]
    assert result["devices_deferred"] == ["5"]
    breaker_logs = [
        c for c in log.await_args_list
        if c.kwargs["level"] == LogLevel.WARNING and "circuit breaker" in c.kwargs["title"].lower()
    ]
    assert len(breaker_logs) == 1


@pytest.mark.asyncio
async def test_breaker_counter_resets_on_success(
    mocker, lotek_integration, pull_config, mock_redis
):
    # Failures interleaved with successes are per-device noise, not an outage.
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_MAX", 0)
    get_positions, _, _, _ = _setup_pull_mocks(
        mocker, mock_redis, _devices("1", "2", "3", "4", "5")
    )
    fail = [httpx.ReadTimeout("")] * RETRY_ATTEMPTS
    get_positions.side_effect = fail + [[]] + fail + [[]] + fail  # F S F S F
    result = await action_pull_observations(lotek_integration, pull_config)
    assert result["devices_deferred"] == []
    assert result["devices_failed"] == ["1", "3", "5"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/python -m pytest app/actions/tests/test_handlers.py -q -k breaker`
Expected: FAIL — device 5 not deferred.

- [ ] **Step 3: Implement**

In `RunGuards.should_stop`, after the deadline branch:

```python
        if self.consecutive_transport_failures >= BREAKER_THRESHOLD:
            return "circuit breaker"
```

In `_head_pass_device`, split the fetch `except Exception` into:

```python
    except httpx.TransportError as e:
        message = (...same message as the generic fetch failure...)
        logger.warning(message, exc_info=True)
        await log_action_activity(..., level=LogLevel.WARNING)
        guards.record(transport_failure=True)
        return 0, True, state
    except Exception as e:
        ...existing WARNING block...
        guards.record(transport_failure=False)
        return 0, True, state
```

and call `guards.record(transport_failure=False)` on the success path (after the checkpoint save succeeds). Delivery/checkpoint failures also `record(False)` — the breaker watches Lotek, not Gundi.

- [ ] **Step 4: Run the full suite**

Run: `./venv/bin/python -m pytest app -q`
Expected: PASS.

- [ ] **Step 5: Flip-verify**

Change `BREAKER_THRESHOLD` to 30 → trip test fails. Restore to 3.

- [ ] **Step 6: Commit**

```bash
git add app/actions/handlers.py app/actions/tests/test_handlers.py
git commit -m "feat: per-run circuit breaker on consecutive Lotek transport failures"
```

---

### Task 10: `backfill_observations` internal action

Lease-guarded, least-recently-backfilled-first, max 2 windows per device per run, gap advanced only on delivered windows, same rails.

**Files:**
- Modify: `app/actions/handlers.py`
- Test: `app/actions/tests/test_handlers.py` (new section) — or `app/actions/tests/test_backfill.py` for readability; prefer the separate file.

**Interfaces:**
- Consumes: `DeviceState`, `BackfillObservationsConfig`, `RunGuards`, `_log_deferral`, `_retry_attempts`, `BACKFILL_WINDOW`, `BACKFILL_MAX_WINDOWS_PER_DEVICE`, `BACKFILL_LEASE_SOURCE`, `state_manager.set_if_absent` / `delete_state`.
- Produces: `async def action_backfill_observations(integration, action_config: BackfillObservationsConfig)` returning `{'observations_extracted': int, 'devices_failed': list, 'devices_deferred': list, 'gaps_closed': int}` or `{'skipped': 'lease_held'}`; helper `async def _backfill_device(device, state, integration, auth, pull_config, guards) -> tuple[int, bool, bool]` (`sent, device_failed, gap_closed`).

- [ ] **Step 1: Write the failing tests** (`app/actions/tests/test_backfill.py`)

```python
import pytest
import httpx
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from gundi_core.schemas.v2 import LogLevel
from app.actions.handlers import (
    BACKFILL_MAX_WINDOWS_PER_DEVICE,
    RETRY_ATTEMPTS,
    action_backfill_observations,
)
from app.actions.client import LotekDevice, LotekException
from app.actions.configurations import BackfillObservationsConfig


def _devices(*device_ids):
    return [
        LotekDevice(
            nDeviceID=d, strSpecialID="s", dtCreated=datetime.now(), strSatellite="sat"
        )
        for d in device_ids
    ]


def _gap_state(days_back_start=7, days_back_end=1, last_backfilled=None):
    now = datetime.now(timezone.utc)
    state = {
        "high_water": now.isoformat(),
        "gap_start": (now - timedelta(days=days_back_start)).isoformat(),
        "gap_end": (now - timedelta(days=days_back_end)).isoformat(),
    }
    if last_backfilled:
        state["last_backfilled"] = last_backfilled.isoformat()
    return state


def _setup_backfill_mocks(mocker, mock_redis, devices, states_by_device):
    # lotek_integration fixture only carries an auth config; backfill also
    # reads the pull config for max_pdop — patch the config getters instead.
    mocker.patch("app.services.state.redis", mock_redis)
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch("app.actions.client.get_token", new=AsyncMock(return_value="token"))
    mocker.patch("app.actions.client.get_devices", new=AsyncMock(return_value=devices))
    from app.actions.configurations import PullObservationsConfig
    mocker.patch("app.actions.handlers.get_pull_config", return_value=PullObservationsConfig())
    get_positions = mocker.patch(
        "app.actions.client.get_positions", new=AsyncMock(return_value=[])
    )
    mocker.patch(
        "app.services.state.IntegrationStateManager.get_state",
        new=AsyncMock(side_effect=lambda i, a, d: states_by_device.get(d, {})),
    )
    set_state = mocker.patch(
        "app.services.state.IntegrationStateManager.set_state", new=AsyncMock()
    )
    lease = mocker.patch(
        "app.services.state.IntegrationStateManager.set_if_absent",
        new=AsyncMock(return_value=True),
    )
    release = mocker.patch(
        "app.services.state.IntegrationStateManager.delete_state", new=AsyncMock()
    )
    log = mocker.patch("app.actions.handlers.log_action_activity", new=AsyncMock())
    return get_positions, set_state, lease, release, log


@pytest.mark.asyncio
async def test_backfill_skips_whole_run_when_lease_is_held(
    mocker, lotek_integration, mock_redis
):
    get_positions, _, lease, release, _ = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1"), {"1": _gap_state()}
    )
    lease.return_value = False
    result = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig()
    )
    assert result == {"skipped": "lease_held"}
    get_positions.assert_not_awaited()
    release.assert_not_awaited()  # a lease we didn't take is not ours to release


@pytest.mark.asyncio
async def test_backfill_releases_lease_even_when_the_run_raises(
    mocker, lotek_integration, mock_redis
):
    _, _, _, release, _ = _setup_backfill_mocks(mocker, mock_redis, [], {})
    mocker.patch(
        "app.actions.client.get_devices", new=AsyncMock(side_effect=httpx.ReadTimeout(""))
    )
    mocker.patch("app.actions.handlers.RETRY_WAIT_INITIAL", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_JITTER", 0)
    mocker.patch("app.actions.handlers.RETRY_WAIT_MAX", 0)
    with pytest.raises(httpx.ReadTimeout):
        await action_backfill_observations(lotek_integration, BackfillObservationsConfig())
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_backfill_caps_windows_per_device_and_advances_gap_start(
    mocker, lotek_integration, mock_redis
):
    # A 20-day gap needs 3 seven-day windows; only 2 (the cap) may run,
    # oldest-first, and gap_start must advance to the end of the last
    # delivered window.
    get_positions, set_state, _, _, _ = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1"), {"1": _gap_state(days_back_start=21)}
    )
    await action_backfill_observations(lotek_integration, BackfillObservationsConfig())
    assert get_positions.await_count == BACKFILL_MAX_WINDOWS_PER_DEVICE
    first_start = get_positions.await_args_list[0].args[3]
    second_start = get_positions.await_args_list[1].args[3]
    assert second_start - first_start == timedelta(days=7)
    final_state = set_state.await_args_list[-1].args[2]
    assert final_state["gap_start"] - first_start == timedelta(days=14)
    assert final_state["gap_end"] is not None  # 20d gap: still open after 2 windows


@pytest.mark.asyncio
async def test_backfill_closes_gap_to_null_when_fully_covered(
    mocker, lotek_integration, mock_redis
):
    get_positions, set_state, _, _, _ = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1"), {"1": _gap_state(days_back_start=5)}
    )
    result = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig()
    )
    assert get_positions.await_count == 1  # 4-day gap fits one 7-day window
    final_state = set_state.await_args_list[-1].args[2]
    assert final_state["gap_start"] is None and final_state["gap_end"] is None
    assert final_state["last_backfilled"] is not None
    assert result["gaps_closed"] == 1


@pytest.mark.asyncio
async def test_backfill_orders_devices_least_recently_backfilled_first(
    mocker, lotek_integration, mock_redis
):
    now = datetime.now(timezone.utc)
    states = {
        "recent": _gap_state(last_backfilled=now - timedelta(minutes=5)),
        "never": _gap_state(),  # no last_backfilled → leads
        "old": _gap_state(last_backfilled=now - timedelta(days=2)),
    }
    get_positions, _, _, _, _ = _setup_backfill_mocks(
        mocker, mock_redis, _devices("recent", "never", "old"), states
    )
    await action_backfill_observations(lotek_integration, BackfillObservationsConfig())
    order = [c.args[0] for c in get_positions.await_args_list]
    assert order == ["never", "old", "recent"]


@pytest.mark.asyncio
async def test_backfill_does_not_advance_gap_when_send_fails(
    mocker, lotek_integration, lotek_position, mock_redis
):
    get_positions, set_state, _, _, log = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1", "2"), {"1": _gap_state(), "2": _gap_state()}
    )
    get_positions.return_value = [lotek_position]
    mocker.patch(
        "app.actions.handlers.gundi_tools.send_observations_to_gundi",
        new=AsyncMock(side_effect=[Exception("boom"), None]),
    )
    result = await action_backfill_observations(
        lotek_integration, BackfillObservationsConfig()
    )
    assert result["devices_failed"] == ["1"]
    # device 1's gap untouched; only device 2 checkpointed a gap advance
    gap_saves = [c for c in set_state.await_args_list if c.args[3] == "1" and c.args[2].get("gap_start") is None]
    assert not gap_saves or all(c.args[2].get("gap_end") is not None for c in gap_saves)
    error_logs = [c for c in log.await_args_list if c.kwargs["level"] == LogLevel.ERROR]
    assert error_logs  # delivery failures stay ERROR in backfill too


@pytest.mark.asyncio
async def test_backfill_skips_devices_without_gaps(
    mocker, lotek_integration, mock_redis
):
    now = datetime.now(timezone.utc)
    states = {"1": {"high_water": now.isoformat()}, "2": _gap_state()}
    get_positions, _, _, _, _ = _setup_backfill_mocks(
        mocker, mock_redis, _devices("1", "2"), states
    )
    await action_backfill_observations(lotek_integration, BackfillObservationsConfig())
    assert [c.args[0] for c in get_positions.await_args_list] == ["2"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/python -m pytest app/actions/tests/test_backfill.py -q`
Expected: FAIL — `ImportError: action_backfill_observations`.

- [ ] **Step 3: Implement**

In `app/actions/handlers.py` (import `BackfillObservationsConfig` alongside the other configs):

```python
async def _backfill_device(device, state, integration, auth, pull_config, guards):
    """Close up to BACKFILL_MAX_WINDOWS_PER_DEVICE oldest windows of one
    device's gap. Returns (observations_sent, device_failed, gap_closed).
    gap_start advances only past windows that were actually delivered, so a
    failure re-fetches the same window next trigger (re-sends are tolerated,
    silent skips are not)."""
    integration_id = str(integration.id)
    observations_sent = 0
    device_failed = False
    for _ in range(BACKFILL_MAX_WINDOWS_PER_DEVICE):
        if not state.has_gap:
            break
        upper_date = min(state.gap_end, state.gap_start + BACKFILL_WINDOW)
        try:
            async for attempt in stamina.retry_context(
                on=RETRYABLE_ERRORS, attempts=_retry_attempts(guards.run_started_at),
                wait_initial=RETRY_WAIT_INITIAL, wait_jitter=RETRY_WAIT_JITTER, wait_max=RETRY_WAIT_MAX,
            ):
                with attempt:
                    positions = await client.get_positions(
                        device.nDeviceID, auth, integration, state.gap_start, upper_date, True
                    )
            cdip_positions = filter_and_transform_positions(positions, integration, pull_config)
        except LotekUnauthorizedException:
            raise
        except httpx.TransportError as e:
            message = (
                f"Error fetching backfill positions from Lotek. Device: {device.nDeviceID}. "
                f"Dates: [{state.gap_start},{upper_date}]. Integration ID: {integration_id} "
                f"Exception: {describe_exception(e)}"
            )
            logger.warning(message, exc_info=True)
            await log_action_activity(
                integration_id=integration_id, action_id="backfill_observations",
                title=message, level=LogLevel.WARNING,
            )
            guards.record(transport_failure=True)
            device_failed = True
            break
        except Exception as e:
            message = (
                f"Error fetching backfill positions from Lotek. Device: {device.nDeviceID}. "
                f"Dates: [{state.gap_start},{upper_date}]. Integration ID: {integration_id} "
                f"Exception: {describe_exception(e)}"
            )
            logger.warning(message, exc_info=True)
            await log_action_activity(
                integration_id=integration_id, action_id="backfill_observations",
                title=message, level=LogLevel.WARNING,
            )
            guards.record(transport_failure=False)
            device_failed = True
            break

        try:
            for batch in generate_batches(cdip_positions):
                await gundi_tools.send_observations_to_gundi(
                    observations=batch, integration_id=integration.id
                )
                observations_sent += len(batch)
        except Exception as e:
            message = (
                f"Error delivering backfill observations for device {device.nDeviceID}. "
                f"Integration ID: {integration_id} Exception: {describe_exception(e)}"
            )
            logger.exception(message, extra={
                'needs_attention': True,
                'integration_id': integration_id,
                'action_id': "backfill_observations",
            })
            await log_action_activity(
                integration_id=integration_id, action_id="backfill_observations",
                title=message, level=LogLevel.ERROR,
            )
            guards.record(transport_failure=False)
            device_failed = True
            break

        # Advance the gap only past the delivered window; close it when done.
        state.gap_start = upper_date
        if not state.has_gap:
            state.gap_start = None
            state.gap_end = None
        await state_manager.set_state(
            integration_id, "pull_observations", state.dict(), device.nDeviceID
        )

    if not device_failed:
        guards.record(transport_failure=False)
    state.last_backfilled = datetime.now(tz=timezone.utc)
    await state_manager.set_state(
        integration_id, "pull_observations", state.dict(), device.nDeviceID
    )
    return observations_sent, device_failed, not state.has_gap


@action_title("Backfill Observations")
@activity_logger()
async def action_backfill_observations(integration, action_config: BackfillObservationsConfig):
    """Internal action: closes per-device historical gaps opened by the head
    pass, oldest-first, least-recently-backfilled device first. Triggered by
    pull_observations; the Redis lease keeps overlapping triggers from
    double-running when a backfill grinds past the next head-pass trigger."""
    logger.info(f"Executing backfill_observations action with integration {integration}...")
    integration_id = str(integration.id)
    auth = get_auth_config(integration)
    pull_config = get_pull_config(integration)  # max_pdop applies to backfilled data too
    run_started = datetime.now(tz=timezone.utc)

    got_lease = await state_manager.set_if_absent(
        integration_id, "backfill_observations",
        ttl_seconds=settings.MAX_ACTION_EXECUTION_TIME,
        source_id=BACKFILL_LEASE_SOURCE,
    )
    if not got_lease:
        logger.info(f"Backfill lease already held for integration {integration_id}; skipping run.")
        return {"skipped": "lease_held"}

    try:
        try:
            async for attempt in stamina.retry_context(
                on=RETRYABLE_ERRORS, attempts=RETRY_ATTEMPTS,
                wait_initial=RETRY_WAIT_INITIAL, wait_jitter=RETRY_WAIT_JITTER, wait_max=RETRY_WAIT_MAX,
            ):
                with attempt:
                    device_list = await client.get_devices(integration, auth)
        except Exception as e:
            message = (
                f"Error fetching devices from Lotek for backfill. Integration ID: "
                f"{integration_id} Exception: {describe_exception(e)}"
            )
            logger.exception(message)
            await log_action_activity(
                integration_id=integration_id, action_id="backfill_observations",
                title=message, level=LogLevel.ERROR,
            )
            raise e

        gapped = []
        for device in device_list:
            saved = await state_manager.get_state(
                integration_id, "pull_observations", device.nDeviceID
            )
            if not saved:
                continue
            try:
                state = DeviceState.parse_obj(saved)
            except pydantic.ValidationError:
                continue  # head pass will initialize it; nothing to backfill yet
            if state.has_gap:
                gapped.append((device, state))
        # Least-recently-backfilled first keeps the import fair; devices never
        # backfilled lead the queue.
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        gapped.sort(key=lambda pair: pair[1].last_backfilled or epoch)

        guards = RunGuards(run_started)
        observations_extracted = 0
        failed_devices = []
        deferred_devices = []
        serviced_devices = 0
        gaps_closed = 0
        for i, (device, state) in enumerate(gapped):
            if reason := guards.should_stop():
                deferred_devices = [d.nDeviceID for d, _ in gapped[i:]]
                await _log_deferral(integration, "backfill_observations", reason, deferred_devices)
                break
            try:
                sent, device_failed, gap_closed = await _backfill_device(
                    device, state, integration, auth, pull_config, guards
                )
            except LotekUnauthorizedException:
                raise
            except Exception as e:
                message = (
                    f"Failed to backfill device {device.nDeviceID} for integration "
                    f"{integration_id}: {describe_exception(e)}"
                )
                logger.exception(message)
                await log_action_activity(
                    integration_id=integration_id, action_id="backfill_observations",
                    title=message, level=LogLevel.ERROR,
                )
                failed_devices.append(device.nDeviceID)
                guards.record(transport_failure=False)
                continue
            observations_extracted += sent
            gaps_closed += int(gap_closed)
            if device_failed:
                failed_devices.append(device.nDeviceID)
            else:
                serviced_devices += 1

        if gapped and serviced_devices == 0 and observations_extracted == 0:
            raise LotekException(
                message=(
                    f"No devices could be backfilled for integration {integration_id}: "
                    f"{len(failed_devices)} failed, {len(deferred_devices)} deferred of "
                    f"{len(gapped)}. See the per-device errors in this action's activity log."
                )
            )

        return {
            'observations_extracted': observations_extracted,
            'devices_failed': failed_devices,
            'devices_deferred': deferred_devices,
            'gaps_closed': gaps_closed,
        }
    finally:
        await state_manager.delete_state(
            integration_id, "backfill_observations", BACKFILL_LEASE_SOURCE
        )
```

- [ ] **Step 4: Run the full suite**

Run: `./venv/bin/python -m pytest app -q`
Expected: PASS, < 2 s.

- [ ] **Step 5: Flip-verify two critical lines**

1. Change `state.gap_start = upper_date` to `state.gap_start = state.gap_start` → window-cap test fails on the final-state assertion. Restore.
2. Swap the sort to `reverse=True` → LRS-ordering test fails. Restore byte-identical.

- [ ] **Step 6: Commit**

```bash
git add app/actions/handlers.py app/actions/tests/test_backfill.py
git commit -m "feat: internal backfill_observations action — lease, LRS ordering, 2-window cap"
```

---### Task 11: Trigger wiring + registration checks

The trigger block landed in Task 6; now pin its behavior and the internal-action registration exclusion.

**Files:**
- Modify: none expected (tests only; fix if tests reveal gaps)
- Test: `app/actions/tests/test_handlers.py`, `app/actions/tests/test_backfill.py`

**Interfaces:**
- Consumes: `trigger_action` (patched in tests at `app.actions.handlers.trigger_action`), `get_actions()` from `app.actions.core`.

- [ ] **Step 1: Write the tests**

```python
@pytest.mark.asyncio
async def test_head_pass_triggers_backfill_when_gap_open_and_lease_free(
    mocker, lotek_integration, pull_config, mock_redis
):
    # First run on default config opens a gap → backfill must be triggered.
    _setup_pull_mocks(mocker, mock_redis, _devices("1"))
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    await action_pull_observations(lotek_integration, pull_config)
    trigger.assert_awaited_once_with(str(lotek_integration.id), "backfill_observations")


@pytest.mark.asyncio
async def test_head_pass_does_not_trigger_backfill_when_lease_held(
    mocker, lotek_integration, pull_config, mock_redis
):
    _setup_pull_mocks(mocker, mock_redis, _devices("1"))
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    # get_state is patched per-device in _setup_pull_mocks; re-patch to return
    # the lease sentinel for the lease key and {} otherwise
    mocker.patch(
        "app.services.state.IntegrationStateManager.get_state",
        new=AsyncMock(side_effect=lambda i, a, s="no-source": "1" if s == "lease" else {}),
    )
    await action_pull_observations(lotek_integration, pull_config)
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_head_pass_does_not_trigger_backfill_without_gaps(
    mocker, lotek_integration, pull_config, mock_redis
):
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _setup_pull_mocks(mocker, mock_redis, _devices("1"), saved_state={"high_water": recent})
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    await action_pull_observations(lotek_integration, pull_config)
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_trigger_failure_does_not_fail_the_head_pass(
    mocker, lotek_integration, pull_config, mock_redis
):
    _setup_pull_mocks(mocker, mock_redis, _devices("1"))
    mocker.patch(
        "app.actions.handlers.trigger_action", new=AsyncMock(side_effect=Exception("pubsub down"))
    )
    result = await action_pull_observations(lotek_integration, pull_config)
    assert result["devices_failed"] == []


def test_backfill_action_is_discovered_but_internal():
    from app.actions.core import discover_actions, InternalActionConfiguration
    handlers = discover_actions(module_name="app.actions.handlers", prefix="action_")
    assert "backfill_observations" in handlers
    _, config_model, _ = handlers["backfill_observations"]
    assert issubclass(config_model, InternalActionConfiguration)
```

Note: `_setup_pull_mocks` already patches `trigger_action`; re-patching in a test replaces it — keep the helper's patch and have it *return* the mock instead if that reads better.

- [ ] **Step 2: Run the tests**

Run: `./venv/bin/python -m pytest app/actions/tests -q -k "trigger or discovered"`
Expected: PASS if Task 6/10 wiring is correct; fix any gaps revealed.

- [ ] **Step 3: Dead-code sweep**

`client.IntegrationState` and `client.default_updated_at` are now unused by handlers. Check references:

```bash
grep -rn "IntegrationState\b\|default_updated_at" app --include="*.py" | grep -v device_state | grep -v "IntegrationStateManager"
```

If only `client.py` defines them and no test uses them, delete both from `client.py` (and any orphaned tests). If tests elsewhere still exercise them, leave with a `# TODO(GUNDI-5602): remove with v1 cursor` — do NOT silently keep dead code without the marker.

- [ ] **Step 4: Run the full suite**

Run: `./venv/bin/python -m pytest app -q`
Expected: PASS, < 2 s.

- [ ] **Step 5: Commit**

```bash
git add -A app
git commit -m "test: pin backfill trigger wiring and internal-action registration"
```

---

### Task 12: Final verification, review loop, PR

**Files:**
- Modify: none (verification + PR)

- [ ] **Step 1: Full-suite verification (superpowers:verification-before-completion)**

```bash
./venv/bin/python -m pytest app -q
```

Expected: all green, wall time < 2 s. Also sanity-check schema generation end-to-end:

```bash
./venv/bin/python -c "
from app.actions.configurations import PullObservationsConfig
import json
print(json.dumps(PullObservationsConfig.schema(), indent=2))
print(json.dumps(PullObservationsConfig.ui_schema(), indent=2))
"
```

Expected: `max_data_age_hours` present with min 1 / max 12; ui order lists all four fields; widget `range`.

- [ ] **Step 2: Read-only review loop (superpowers:requesting-code-review)**

Dispatch reviewer agents (`er-developer:gundi-reviewer`) with NO Edit/Write tools against the pinned branch SHA. Freeze commits while a round runs. Max ~3 rounds; stop early when findings dry up. Address findings with the receiving-code-review skill (verify before implementing).

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin feat/GUNDI-5602-head-pass-backfill
gh pr create --repo PADAS/gundi-integration-lotek \
  --title "GUNDI-5602: newest-first head pass + internal backfill with bounded staleness" \
  --reviewer chrisdoehring \
  --body-file /tmp/pr_body.md
```

PR body must include (draft in the scratchpad, then pass via `--body-file`):
- Link to GUNDI-5602 and the spec file in-repo.
- The architecture summary (head pass / internal backfill / single shrinking gap / lease).
- **The bounded-staleness trade-off stated explicitly**: data older than `max_data_age_hours` that could not be fetched is dropped permanently (WARNING with device + range). This supersedes the earlier "no data loss, only delay" framing — an agreed design decision: gaps can't grow, catch-up can't compound, rangers always get fresh positions. Default 12 h with a 10-min cadence ≈ 72 missed runs of slack.
- Safety rails: soft deadline (80% of 540 s), circuit breaker (K=3 consecutive transport failures), zero-progress raise, backfill lease (NX + TTL).
- Error-semantics table (what stays ERROR vs demoted to WARNING) and why (health = ERROR count in 60 min).
- Load math: steady state 1 request/device/run vs up to ~27 today.
- Thundering-herd note: `crontab_schedule` is type-wide with no per-integration jitter (verified in `app/services/action_scheduler.py`) — all 28 integrations still fire the same minutes; out of scope here, but the head pass shrinks each burst to its floor. Fewer, better-spaced requests is also the fastest path to fewer timeouts.
- `🤖 Generated with [Claude Code](https://claude.com/claude-code)` footer.

- [ ] **Step 4: Jira**

Comment on GUNDI-5602 with the PR link. If a new ticket is created for anything, GUNDI project requires `customfield_11524 = {"id": "13872"}` (Gundi Theme: Connector work).

---

## Deliberately out of scope (do NOT fold into this branch)

- Disabling the 5 login-broken integrations (needs a fresh bearer token from Victor; separate prod operation, recorded in `lotek_cutover_ALL.json`).
- The action-runner `_handle_error` `config_data` credential leak — separate upstream ticket in `PADAS/gundi-integration-action-runner`; raise with Victor first.
- Per-integration schedule stagger (template limitation, noted in PR body).
- Post-deploy verification (`lotek_health_check.py`) — after merge + release, not part of the PR.

## Self-review notes (spec coverage)

- Head pass window formula, `high_water` advance → Task 6. First-run gap + no-gap case → Tasks 6.
- Stale-drop WARNING, gap never grows → Task 6 (+ flip-verify).
- Backfill: lease, LRS, N=2 cap, `gap_start` advances only on delivered windows, gap→null → Task 10.
- Lease no-op on second trigger / reclaim on expiry → Task 10 (expiry is Redis TTL, exercised via `set_if_absent` contract — already covered by `test_state_manager.py`).
- Deadline / breaker / zero-progress in BOTH actions → Tasks 7–9 wire pull; Task 10's implementation reuses `RunGuards` + zero-progress predicate; backfill deferral covered by lease/breaker tests.
- Retry posture (2 attempts, deadline-gated, none after breaker trip — the breaker stops the loop before any further fetch) → Tasks 3, 8, 9.
- Migration `updated_at` → `high_water` → Tasks 4, 6.
- Config fields + ui:order + slider → Task 5.
- Error-level table → Tasks 6 (WARNING demotion), 7 (zero-progress ERROR), 8–9 (deferral WARNINGs).
- `describe_exception` fix → Task 2. Pending edits → Task 1.
