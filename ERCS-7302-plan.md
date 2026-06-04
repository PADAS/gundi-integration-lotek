# ERCS-7302 — Lotek PDOP Filter Implementation Plan

## Background

The Lotek API returns a `PDOP` (Positional Dilution of Precision) value with every GPS fix.
A customer wants to only receive fixes with `PDOP <= 4` (more accurate fixes) in EarthRanger,
before data is forwarded via ER-ER share.

**Good news already confirmed:** `PDOP` is already present in the `additional` field of every
observation sent to Gundi today (via `position.dict(exclude={...})` in `filter_and_transform_positions`).
No change needed for that part of the request.

## Changes Required

### 1. `app/actions/configurations.py` — Add `max_pdop` field

Add an optional `max_pdop` field to `PullObservationsConfig`:

```python
class PullObservationsConfig(PullActionConfiguration):
    max_pdop: Optional[float] = pydantic.Field(
        None,
        title="Max PDOP",
        description=(
            "If set, only observations with PDOP <= this value will be sent. "
            "Leave blank to send all observations."
        ),
    )
```

- Defaults to `None` so existing integrations are unaffected unless the field is configured.
- Operators set this per-integration in the Gundi portal.

### 2. `app/actions/handlers.py` — Apply filter in `filter_and_transform_positions`

Change the function signature to accept `action_config` and skip positions that exceed `max_pdop`:

```python
def filter_and_transform_positions(positions, integration, action_config=None):
    max_pdop = action_config.max_pdop if action_config else None
    valid_positions = []
    for position in positions:
        try:
            if position.Longitude is None or position.Latitude is None:
                logger.info(f"Filtering {position} (bad location) for device {position.DeviceID}.")
                continue

            if max_pdop is not None and position.PDOP > max_pdop:
                logger.info(
                    f"Filtering position for device {position.DeviceID} "
                    f"(PDOP={position.PDOP} > max_pdop={max_pdop})."
                )
                continue

            cdip_pos = {
                "source": position.DeviceID,
                "source_name": position.DevName,
                "type": "tracking-device",
                "recorded_at": ensure_timezone_aware(position.RecDateTime).isoformat(),
                "location": {"lat": position.Latitude, "lon": position.Longitude},
                "additional": position.dict(exclude={"DeviceID", "Latitude", "Longitude", "RecDateTime"}),
            }
            valid_positions.append(cdip_pos)
        except Exception as ex:
            logger.error(
                f"Failed to parse Lotek point: {position} for Integration ID {str(integration.id)}. Exception: {ex}"
            )
    return valid_positions
```

Update the call site in `action_pull_observations` to pass `action_config`:

```python
cdip_positions.extend(filter_and_transform_positions(positions, integration, action_config))
```

### 3. `app/actions/tests/test_handlers.py` — Add tests

- Test that positions with `PDOP > max_pdop` are filtered out.
- Test that positions with `PDOP <= max_pdop` are kept.
- Test that when `max_pdop` is `None` (default), all valid positions pass through.
- Test that `PDOP` is present in the `additional` field of the transformed observation.

## Portal Configuration

After deploying, configure the integration for `sdzwaloisaba.pamdas.org` in the Gundi portal:
set `max_pdop = 4` on the `pull_observations` action config for the relevant Lotek integration.

## Notes

- The filter is applied **after** fetching from the API — no Lotek API query param supports DOP filtering.
- `PDOP` (Positional DOP) is a combined x/y/z accuracy measure. The ticket mentions "DOP <= 4" which
  matches `PDOP` in the Lotek payload. No other DOP variant is present in the API response.
