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
    # Schema version: bump on any breaking field change and add a migration in
    # _migrate_legacy_cursor — an unparseable blob costs a full-lookback
    # re-import (review finding), so schema changes must never rely on the
    # parse-failure fallback.
    version: int = 1
    high_water: datetime
    gap_start: Optional[datetime] = None
    gap_end: Optional[datetime] = None
    last_backfilled: Optional[datetime] = None
    # In-memory marker, never persisted (exclude=True): tells the loader this
    # state was parsed from a pre-5602 cursor blob, so a cursor lagging beyond
    # the freshness floor gets its owed range carried over as a gap instead of
    # being dropped by bounded staleness (review finding: on upgrade day a
    # stale legacy cursor lost the range the old code would have caught up).
    migrated_from_legacy: bool = pydantic.Field(False, exclude=True)

    @pydantic.root_validator(pre=True)
    def _migrate_legacy_cursor(cls, values):
        # Pre-5602 state stored the cursor as updated_at; parse it as
        # high_water so deployed integrations carry over without a gap.
        if values.get("high_water") is None and values.get("updated_at") is not None:
            values = dict(values)
            values["high_water"] = values["updated_at"]
            values["migrated_from_legacy"] = True
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
