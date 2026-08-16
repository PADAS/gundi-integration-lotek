import json

import pydantic
import pytest

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
