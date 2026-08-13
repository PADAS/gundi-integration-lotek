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
