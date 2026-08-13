# Lotek connector: newest-first fetching (head pass + internal backfill)

**Ticket**: GUNDI-5602 · **Status**: approved by Victor 2026-08-13 · **Repo**: gundi-integration-lotek

## Problem

A chronically slow Lotek API (`webservice.lotek.com`) times out our 9-minute action budget
(~264 `ACTION_TIMEOUT_9MIN` ERRORs/day across all active connections). The current design
walks each device's backlog oldest-first, so when the budget dies, the freshest positions
are what never arrive — rangers can't see where the animal is *now*. Worse, a slow device's
cursor pins, so it re-queries ever-wider windows next run (up to 9 windows × 3 retries = 27
requests for one device), amplifying Lotek's load and our own timeouts.

## Design

Flip the fetch order: freshest data first, history second, and bound how much history we
ever owe.

### Two actions

**`pull_observations`** (scheduled, cadence unchanged):
1. `get_devices` (ERROR + raise on failure, as today).
2. **Head pass** over every device: fetch `[max(high_water, now − max_data_age_hours), now]`
   (upload-time query, as today), transform, send, set `high_water = now`.
3. If any device has an open gap and the backfill lease is free:
   `trigger_action(integration_id, "backfill_observations")`.

**`backfill_observations`** (internal — `InternalActionConfiguration`, config never shown in
the portal, only triggered by `pull_observations`):
1. Acquire lease: `backfill_lease_until` in integration-level state; skip the whole run if an
   unexpired lease exists (prevents overlapping backfills when a run grinds past the next
   trigger). Lease TTL = `MAX_ACTION_EXECUTION_TIME`.
2. Order gapped devices **least-recently-backfilled first** (persist `last_backfilled` per
   device).
3. Per device: fetch up to **N = 2 windows** (7-day chunks, oldest-first) from
   `[gap_start, gap_end]`, send, advance `gap_start` per delivered window; gap closed →
   null it out.
4. Release lease.

### State per device (Redis, keyed `(integration_id, action_id, device_id)` as today)

| Field | Meaning |
|---|---|
| `high_water` | newest upload-time synced (replaces `updated_at`) |
| `gap_start`, `gap_end` | single unfetched historical range; null when closed |
| `last_backfilled` | LRS ordering key for backfill fairness |

**Migration**: existing `updated_at` → `high_water`, no gap. First head pass after deploy
covers `[max(updated_at, now − max_data_age_hours), now]`; anything older than
`max_data_age_hours` that the old cursor still owed is dropped (see trade-off below).

**Gap lifecycle**: created **once**, on a device's first run —
`[now − default_lookback_days, now − max_data_age_hours]`, the deliberate historical import.
It only ever shrinks. It is never extended, so there is no merge logic and no multi-gap
bookkeeping.

### Bounded staleness (the deliberate trade-off)

In steady state the head pass never reaches back more than `max_data_age_hours`. If a device
falls further behind (Lotek outage, repeated head-pass failures, integration disabled a
while), the span `[high_water, now − max_data_age_hours]` is **dropped permanently** with a
WARNING naming the device and range — it is not added to the gap.

Consequences, all intended:
- The gap cannot grow → per-device catch-up cost cannot compound → runtime converges.
- Rangers always get recent positions; history is best-effort.
- **Data loss is possible** when an outage exceeds `max_data_age_hours`. This supersedes the
  earlier "deferral = no data loss, only delay" framing; Victor's explicit call. The 12h
  default with a 10-min cadence gives ~72 missed runs of slack before anything is lost.

ER handles out-of-order arrival fine (live position = newest `recorded_at` regardless of
arrival order), so late backfill infill is harmless. All windows are defined in **upload
time** (the existing query semantics), so collars that upload old points in bursts are not
missed by the head pass.

### Portal configuration (`PullObservationsConfig`)

| Param | Default | Range | Notes |
|---|---|---|---|
| `default_lookback_days` | 7 | 1–60 | existing; description narrows to "historic data imported on the first run" |
| `max_data_age_hours` | 12 | 1–12 | **new**; slider (`ui:widget: range`); "positions uploaded longer ago than this that could not be fetched are skipped permanently" |
| `max_pdop` | — | ≥0 | unchanged |
| `run_on_schedule` | true | hidden | unchanged; keep in `ui:order` |

Not configurable (code constants — load-protection mechanics, not operator knobs): window
cap N=2, deadline fraction, breaker K=3, retry attempts, window chunk size (7d), lease TTL.

### Safety rails (both actions)

- **Deadline**: stop starting new device work past ~80% of `MAX_ACTION_EXECUTION_TIME`
  (540s → ~430s). Controlled exit: WARNING activity log + `devices_deferred` result key.
  Never end via the `asyncio.wait_for` cancellation.
- **Circuit breaker**: K=3 consecutive devices failing on timeout/transport errors → assume
  Lotek-wide degradation, stop early, defer the remainder (WARNING). Also bounds login
  storms.
- **Zero-progress guard**: a run that services zero devices raises (ERROR) like the
  all-failed case — systemic degradation must alert, not warn forever.
- **Retry posture**: `RETRY_ATTEMPTS` 3 → 2 for position fetches; zero retries once the
  breaker has tripped; Lotek-5xx retries gated on the deadline.

### Error semantics (health keys on ERROR count only — cdip `calculate_integration_status`)

| Event | Level |
|---|---|
| login refused, `get_devices` failure, all-heads-failed, delivery failure, zero-progress | ERROR |
| per-device transient fetch timeout, deferrals (deadline/breaker), dropped stale ranges | WARNING |

### Starvation

Freshness starvation is impossible by construction: every device is head-fetched every run.
Backfill fairness = LRS ordering + per-device window cap; a deadline-cut tail rotates to the
front of the next trigger. Worst-case import delay ≈ gapped-devices ÷ (budget ÷ window-cost)
× cadence, and the import is finite.

### Load (this is also the Lotek load-relief fix)

Steady state = **1 request/device/run**, the theoretical floor (vs. up to ~27 today for a
pinned device). Backfill is a one-time bounded import — a 7-day lookback is a single window
per device. Fewer, better-spaced requests is also the fastest path to fewer timeouts for us.

## Testing

TDD per the practices established this week: failing test first; flip-verify assertions on
critical lines; suite < 2s (`RETRY_WAIT_INITIAL`/`RETRY_WAIT_JITTER` patched to 0). Key
behaviors to pin:

- head pass sends newest window and advances `high_water` even with an open gap
- first-run gap creation `[now − lookback, now − max_age]`; no gap when lookback ≤ max_age
- steady-state: no gap opened when `high_water` within `max_data_age_hours`
- stale-span drop: outage > max_age → WARNING with range, gap unchanged
- backfill: LRS ordering, N-window cap, `gap_start` advances only on delivered windows,
  gap closes to null
- lease: second trigger while lease held is a no-op; expired lease is reclaimed
- deadline/breaker/zero-progress in both actions
- migration: legacy `updated_at` state parses as `high_water`

## Out of scope (tracked elsewhere in PROMPT_lotek_followup.md)

- Thundering-herd stagger across the 28 integrations (verify `crontab_schedule` support
  before the PR; open question, not part of this design).
- Disabling the 5 login-broken integrations (task 1).
- The action-runner template `config_data` credential-leak fix (separate ticket, upstream).
