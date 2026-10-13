# Readable fetch-failure logs: implementation plan

## Goal

When some devices fail during a pull, the portal activity log shows one generic warning:

> Pulled observations with 1 of 25 device(s) failing for integration 8c5df1ae-…: 88630. They will be retried on the next run.

It is not clear in which part of the process the error occurred, or why.

After this change, each warning says what happened, in plain words:

> Lotek didn't send data for device 88630 because the connection timed out.

Messages report only what happened in that run. They don't predict later runs (for example "will be retried" or "nothing was lost"), because a log can't know that, and old logs are never edited afterwards. Wording stays direct and non-technical.

## Failure reasons

Each failed device gets exactly one reason:

| Reason | Where it comes from | Portal message (per group of devices) |
|---|---|---|
| `timeout` | `httpx.TimeoutException` in `_fetch_window` | Lotek didn't send data for device(s) {ids} because the connection timed out. |
| `no_connection` | any other `httpx.TransportError` in `_fetch_window` | Lotek didn't send data for device(s) {ids} because Gundi couldn't connect to it. |
| `lotek_error` | `LotekException` with 5xx or 429 | Lotek didn't send data for device(s) {ids} because its server returned an error. |
| `login_rejected` | `LotekException` with 401 (token expiry that survived its retry) | Lotek didn't send data for device(s) {ids} because it rejected Gundi's login. |
| `delivery_failed` | `_deliver` failed (sending to Gundi) | Lotek sent data for device(s) {ids}, but Gundi couldn't receive it. |
| `unexpected` | other 4xx, data-shape errors, and exceptions caught by the traversal | Something unexpected went wrong getting data for device(s) {ids}. See the error above in this log. |

The backfill uses the same messages; the activity log already shows which action wrote them.

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
- `test_backfill.py`: the backfill summary uses the same messages; update the mocks of `_fetch_window` and `_backfill_device` to return the new tuple shapes.
- `test_sharding.py`: update the `_head_pass_device` mocks to return the four-element tuple.
