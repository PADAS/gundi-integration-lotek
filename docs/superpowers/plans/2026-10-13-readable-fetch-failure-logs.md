# Readable fetch-failure logs: implementation plan

## Goal

When some devices fail during a pull, the portal activity log shows one generic warning:

> Pulled observations with 1 of 25 device(s) failing for integration 8c5df1ae-…: 88630. They will be retried on the next run.

It says that something failed, but not what happened or whether data is missing. For example, the server logs showed that this specific warning came from a `ConnectTimeout`: Lotek didn't answer in time. The next run asked again for the same time window and got through normally, so nothing was lost. None of that is visible in the portal.

After this change, each warning names the cause in plain words and says what happens next:

> Timed out while waiting for Lotek to provide data for device 88630. Gundi will ask for the same data again on the next run.

## Failure reasons

Each failed device gets exactly one reason:

| Reason | Where it comes from | Portal message (per group of devices) |
|---|---|---|
| `timeout` | `httpx.TimeoutException` in `_fetch_window` | Timed out while waiting for Lotek to provide data for device(s) {ids}. Gundi will ask for the same data again on the next run. |
| `no_connection` | any other `httpx.TransportError` in `_fetch_window` | Could not connect to Lotek for device(s) {ids}. Gundi will ask for the same data again on the next run. |
| `lotek_error` | `LotekException` with 5xx or 429 | Lotek's server returned an error for device(s) {ids}. Gundi will ask for the same data again on the next run. |
| `login_rejected` | `LotekException` with 401 (token expiry that survived its retry) | Lotek rejected Gundi's login for device(s) {ids}. Gundi will log in again and retry on the next run. |
| `delivery_failed` | `_deliver` failed (sending to Gundi) | Could not send data to Gundi for device(s) {ids}. It will be fetched and sent again on the next run. |
| `unexpected` | other 4xx, data-shape errors, and exceptions caught by the traversal | Unexpected error getting data for device(s) {ids}. See the error above in this log. Gundi will retry on the next run. |

The head pass says "on the next run". The backfill says "on the next backfill" instead, which matches how its retries work today.

## Changes

### `app/actions/handlers.py`

1. Add the reason constants and a `FAILURE_MESSAGES` mapping from reason to message template, next to `_summarize_ids`.
2. `_fetch_window`: return `(None, transport_failure, reason)` on failure, with the reason picked by the `except` branch it falls into (table above). Success returns `(positions, False, None)`. Retries, breaker feeding and log levels stay as they are.
3. `_head_pass_device`: return `(sent, device_failed, transport_failure, failure_reason)`. Pass through the reason from `_fetch_window`, or `delivery_failed` when `_deliver` fails.
4. `_backfill_device`: add `failure_reason` to its return tuple the same way.
5. Head-pass and backfill loops: call `traversal.mark_failed(device_id, reason)`.
6. Replace the single "Pulled observations with N of M device(s) failing…" warning, and the matching "Backfilled with…" warning, with one warning per reason that occurred, built from `FAILURE_MESSAGES` and `_summarize_ids`.

### `app/actions/traversal.py`

1. `DeviceTraversal` keeps `failure_reasons: dict[device_id, reason]` alongside `failed_devices`.
2. `mark_failed(device_id, reason="unexpected")` records both.
3. Exceptions caught inside `run()` are recorded as `unexpected`. They already get their own ERROR line, which the `unexpected` message points to.
4. Add `failed_by_reason()`, which returns devices grouped by reason in a stable order, so the handlers can build the messages.

`failed_devices` and the `devices_failed` result field keep their current shape, so the result payload and the zero-progress check don't change.

## Tests

- `test_traversal.py`: `mark_failed` with and without a reason; exceptions in `run()` are recorded as `unexpected`; `failed_by_reason()` grouping and order.
- `test_handlers.py`: one test per reason for `_fetch_window` (timeout, connect error, 5xx, 429, 401, other 4xx, malformed data); the shard emits one warning per reason with the expected text; update the existing summary tests (`test_action_pull_observations_summary_names_failed_devices` and the assertion that matches "failing for integration").
- `test_backfill.py`: the backfill summary uses the backfill wording; update the mocks of `_fetch_window` and `_backfill_device` to return the new tuple shapes.
- `test_sharding.py`: update the `_head_pass_device` mocks to return the four-element tuple.
