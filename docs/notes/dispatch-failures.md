# "Gundi couldn't start getting data" dispatch failures

## What was seen

On 2026-11-12 the `pull_observations` dispatcher could not hand any device off to `pull_observations_shard`, and the portal showed:

- **Warning:** "Gundi couldn't start getting data for 55 device(s) in this run: 88630, 88631, 88633, …"
- **Error:** "Gundi couldn't start getting data for any device in this run."

| Time (UTC) | Integration | Devices not dispatched |
|---|---|---|
| 2026-11-12 13:55:02 | `19e00193-ca73-4549-8865-12ce7eca60a3` | 5 of 5 |
| 2026-11-12 21:24:22 | `8c5df1ae-7940-4979-b350-c224f4459507` (CDFW Big Game Lotek) | 55 of 55 |

At 21:24:22 the `lotek-actions-runner` logs show `asyncio.TimeoutError` from `gcloud.aio.pubsub` `publisher_client.publish` (via `app/services/activity_logger.py` `publish_event`): the Pub/Sub publish timed out. The next runs of `8c5df1ae…` that day dispatched normally.

Log query (project `cdip-prod1-78ca`):

```
resource.type="cloud_run_revision"
resource.labels.service_name="lotek-actions-runner"
textPayload:"couldn't start getting data"
```

## To research

- How often this happens, for which integrations, and at what times (fleet size, schedule ticks, other runners publishing at the same moment).
- Whether the cause is always a Pub/Sub publish timeout, or other publish errors too.
- Whether the publish timeout or `DISPATCH_CONCURRENCY` / `_bounded_publish` should change.

## Log improvement

The portal message doesn't say why the hand-off failed. Add the reason in plain words, e.g. "Gundi couldn't start getting data for any device in this run because its message queue didn't respond in time." Code: `app/actions/handlers.py`, the `undispatched_devices` / `dispatched == 0` block at the end of the dispatcher.
