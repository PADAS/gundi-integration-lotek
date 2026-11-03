# Readable fetch-failure logs: implementation plan

## Goal

When some devices fail during a pull, the portal activity log shows one generic warning:

> Pulled observations with 1 of 25 device(s) failing for integration 8c5df1ae-…: 88630. They will be retried on the next run.

It is not clear in which part of the process the error occurred, or why. Short Lotek hiccups also mark the route unhealthy, while problems that last for hours look the same as a one-off blip.

After this change:

- Each warning says what happened, in plain words:
  > Lotek didn't send data for 1 of 25 devices (88630) because the connection timed out.
- A real error is logged as an ERROR and makes the route unhealthy. Lesser situations are warnings.
- A temporary failure becomes an ERROR only when the same device keeps failing for over 1 hour.

Messages report only what happened. They don't predict later runs (for example "will be retried" or "nothing was lost"), because a log can't know that, and old logs are never edited afterwards. Wording stays direct and non-technical.

## Failure reasons

Each failed device gets exactly one reason. Reasons and messages live in one module, `app/actions/failures.py`, as a string `Enum` plus message tables. An unknown reason falls back to `unexpected` instead of breaking the summary.

| Reason | Where it comes from | Kind | Portal message (per group of devices) |
|---|---|---|---|
| `timeout` | `httpx.TimeoutException` in `_fetch_window` | temporary | Lotek didn't send data for {count} because the connection timed out. |
| `no_connection` | any other `httpx.TransportError` in `_fetch_window` | temporary | Lotek didn't send data for {count} because Gundi couldn't connect to it. |
| `lotek_error` | `LotekException` with 5xx or 429 | temporary | Lotek didn't send data for {count} because its server returned an error. |
| `login_rejected` | `LotekException` with 401 (token expiry that survived its retry) | temporary | Lotek didn't send data for {count} because it rejected Gundi's login. |
| `delivery_failed` | `_deliver` failed (sending to Gundi) | error | Lotek sent data for {count}, but Gundi couldn't receive it. |
| `save_failed` | saving the head cursor or the backfill gap failed after delivery | error | Gundi got the latest data for {count} but couldn't save how far it got. |
| `unexpected` | other 4xx, data-shape errors, and exceptions caught by the traversal | error | Something unexpected went wrong getting data for {count}. The error for each device is in this log. |

`{count}` reads "3 of 55 devices (88634, 88635, 88643)", using `_summarize_ids` for the ids. The total is the connection's device count: the dispatcher passes it to each shard as `total_devices` on `PullObservationsShardConfig` (a shard without it falls back to its own size), and the backfill counts against all listed devices.

The backfill uses the same messages; the activity log already shows which action wrote them.

## Log levels

- **Temporary reasons** are local WARNINGs per device and one portal WARNING per reason per run. They don't mark the route unhealthy.
- **Error reasons** keep their per-device ERROR line, plus the per-reason WARNING summary that names them together.
- **A refused login** (`LotekUnauthorizedException`, Gundi can't get a token at all) stays an ERROR with its current integration-wide handling.

### Login rejections

A 401 on a data call already clears the cached token, logs in again and retries. When that new login succeeds and Lotek still answers 401, Gundi can still get tokens, so the device is a `login_rejected` failure: temporary, WARNING, no ERROR line of its own.

### Temporary failures that last over 1 hour

The head pass keeps a per-device `failing_since` time in the device state (new optional field on `DeviceState`, no schema version bump):

- On a temporary failure, if `failing_since` is empty it is set to now and saved.
- On a successful head pass, `failing_since` is cleared in the same save that moves the cursor.
- Error reasons don't change it.

When a device's `failing_since` is more than 1 hour old, its temporary failure is escalated: instead of the WARNING, each run writes one ERROR per reason, grouped like the warnings:

| Reason | Escalated message |
|---|---|
| `timeout` | Lotek hasn't sent data for {count} for over 1 hour because the connection keeps timing out. |
| `no_connection` | Lotek hasn't sent data for {count} for over 1 hour because Gundi keeps failing to connect to it. |
| `lotek_error` | Lotek hasn't sent data for {count} for over 1 hour because its server keeps returning errors. |
| `login_rejected` | Lotek hasn't sent data for {count} for over 1 hour because it keeps rejecting Gundi's login. |

The route stays unhealthy while the problem lasts and recovers on its own once the devices work again. The time is measured from the first failure in a row, not by run count, so it doesn't depend on the schedule. Backfill failures don't escalate: the head pass is what keeps data current, so it is the one that decides.

### Runs where no device succeeds

The existing zero-progress ERROR stays, right away, but says what happened in plain words, grouped by reason:

> None of the 25 devices in this batch could be updated in this run: 20 because the connection timed out, 5 because Lotek rejected Gundi's login.

Deferred devices are added as "N postponed to a later run" when there are any. The message names its scope ("in this batch" for a shard), because it describes one shard only. The backfill's zero-progress ERROR gets the same treatment, scoped "with missing history".

## Changes

### `app/actions/failures.py` (new)

`FailureReason` (`str` `Enum`), `TEMPORARY_REASONS`, `ESCALATION_AFTER = timedelta(hours=1)`, the warning, escalated and short-label message tables, and helpers that build each message with the `unexpected` fallback.

### `app/actions/handlers.py`

1. Named results instead of positional tuples: `FetchResult(positions, transport_failure, failure_reason)`, `HeadPassResult(sent, transport_failure, failure_reason, escalated)` and `BackfillResult(sent, transport_failure, gap_closed, windows_advanced, cut_reason, failure_reason)`. `device_failed` becomes a property (`failure_reason is not None`).
2. `_fetch_window`: picks the reason from the `except` branch it falls into (table above). Retries and breaker feeding stay as they are. A 401 that survived its retry is split out of the "other 4xx" branch: local WARNING, no per-device ERROR activity log, not breaker-feeding.
3. `_head_pass_device`: passes through the fetch reason, or `delivery_failed` / `save_failed`; tracks `failing_since` and sets `escalated`.
4. `_backfill_device`: same reasons; a failed gap save is reported as `save_failed` instead of raising, so the delivered count is kept and the fairness save to the same store is skipped.
5. Head-pass and backfill loops: `traversal.mark_failed(device_id, reason)`; the head pass collects escalated devices.
6. Replace the single "Pulled observations with N of M device(s) failing…" warning, and the matching "Backfilled with…" warning, with one message per reason that occurred: WARNING, or ERROR for escalated devices.
7. Rewrite both zero-progress ERROR messages as above.

### `app/actions/traversal.py`

1. `DeviceTraversal` keeps `failure_reasons: dict[device_id, reason]` alongside `failed_devices`.
2. `mark_failed(device_id, reason=unexpected)` records both.
3. Exceptions caught inside `run()` are recorded as `unexpected`. They already get their own ERROR line.
4. `failed_by_reason()` returns devices grouped by reason in a stable order.

`failed_devices` and the `devices_failed` result field keep their current shape, so the result payload and the zero-progress check don't change.

## Tests

- `test_traversal.py`: `mark_failed` with and without a reason; exceptions in `run()` are recorded as `unexpected`; `failed_by_reason()` grouping and order.
- `test_failures.py`: message building, counts, and the unknown-reason fallback.
- `test_handlers.py`: one test per reason (timeout, connect error, 5xx, 429, 401, other 4xx, malformed data, delivery, cursor save); a 401 that survived its retry writes no ERROR; `failing_since` is set on the first temporary failure, kept on the next, cleared on success; a device failing for over 1 hour gets the escalated ERROR; the zero-progress message names the reasons.
- `test_backfill.py`: the backfill summary uses the same messages; a failed gap save keeps the sent count and skips the fairness save; updated mocks for the named results.
- `test_sharding.py`: updated `_head_pass_device` mocks.
