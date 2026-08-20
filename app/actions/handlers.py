import asyncio
import httpx
import logging
import stamina
import pydantic

import app.services.gundi as gundi_tools
import app.actions.client as client
import app.settings as app_settings
import app.settings.integration as settings

from datetime import datetime, timezone, timedelta

from app.services.errors import ConfigurationNotFound
from app.actions.client import LotekException, LotekTokenExpiredException, LotekUnauthorizedException
from app.services.utils import find_config_for_action
from app.actions.configurations import (
    AuthenticateConfig,
    BackfillObservationsConfig,
    PullObservationsConfig,
    PullObservationsShardConfig,
)
from app.actions.core import action_title, describe_exception
from app.actions.device_state import DeviceState
from app.actions.traversal import DeviceTraversal
from app.services.action_scheduler import trigger_action
from app.services.lotek_connections import lotek_slot, NoConnectionSlot
from app.services.activity_logger import activity_logger, log_action_activity
from app.services.state import IntegrationStateManager
from gundi_core.schemas.v2.gundi import LogLevel

logger = logging.getLogger(__name__)
state_manager = IntegrationStateManager()


# Lotek read timeouts are the common transient failure; retrying them costs one extra
# request but saves a device from being skipped for a whole cycle. Token expiry is here
# because the client clears the cached token before raising, so a retry re-authenticates.
# A refused login (LotekUnauthorizedException) is deliberately NOT retryable: it is
# fatal to the run, and retrying a rejected password risks account lockout.
RETRYABLE_ERRORS = (LotekTokenExpiredException, httpx.TransportError)  # TransportError covers timeouts
RETRY_ATTEMPTS = 2
RETRY_WAIT_INITIAL = 1.0
RETRY_WAIT_JITTER = 5.0
RETRY_WAIT_MAX = 32.0
# A Lotek-wide outage can fail every device; don't put hundreds of ids in a log title.
MAX_DEVICES_IN_SUMMARY = 20
# Safety-rail constants (GUNDI-5602): load-protection mechanics, deliberately
# code constants rather than operator knobs.
DEADLINE_FRACTION = 0.8
BREAKER_THRESHOLD = 3
BACKFILL_MAX_WINDOWS_PER_DEVICE = 2
BACKFILL_WINDOW = timedelta(days=7)
BACKFILL_LEASE_SOURCE = "lease"
# How many devices one invocation processes per chunk. WORK PARTITIONING, not a
# concurrency limit: the account-wide ceiling is LOTEK_MAX_CONNECTIONS, enforced
# in Redis by lotek_slot, which now WAITS rather than refusing. Chunk size may
# freely oversubscribe that ceiling — the budget applies backpressure (spec D1).
FETCH_CONCURRENCY = 5
# How much work fits in one action budget, i.e. how the dispatcher partitions
# the fleet across sub-actions. WORK PARTITIONING, not a concurrency limit —
# see FETCH_CONCURRENCY. Do not shrink this to "fit" LOTEK_MAX_CONNECTIONS;
# that reintroduces the coupling spec D1 removed.
SHARD_SIZE = 25
# Re-trigger governor (review finding, PR #20 discussion): a deferred tail may
# hop to a fresh shard at most this many times before falling back to the next
# scheduled tick. Without the cap, sustained slot starvation turned the
# "pubsub round trip is the backoff" re-trigger into an unbounded busy-loop of
# zero-progress invocations — bounded waste is acceptable, an ungoverned loop
# is not. Exceeding the cap is surfaced at ERROR: it means the account could
# not drain within ~cap budgets and someone should know.
SHARD_RETRIGGER_CAP = 3
# Consecutive whole-tick dispatcher skips on slot starvation before the skip
# is promoted from local log to a portal WARNING. The first skip stays quiet
# (publish-volume discipline); a streak means the account budget has been
# saturated across ticks and must be visible in the portal.
DISPATCHER_SKIP_WARN_AFTER = 2
DISPATCHER_SKIP_STREAK_SOURCE = "slot_skip_streak"
# How many device cursors the dispatcher reads concurrently when ordering the
# fleet. Independent of SHARD_SIZE (this bounds Redis fan-out, not action
# budget) — named so tuning one does not silently look like it covers the other.
STATE_READ_CONCURRENCY = 25
# Outcomes of a deferred-tail re-trigger attempt (see _retrigger_shard).
RETRIGGER_HANDED_OFF = "handed_off"
RETRIGGER_CAP_REACHED = "cap_reached"
RETRIGGER_FAILED = "failed"
# One backfill trigger per head-pass tick: every shard of a gapped fleet used
# to read the lease and publish its own backfill command (the lease is only
# created a pubsub hop later, when the backfill handler runs), so 25 shards
# produced up to 25 commands — 24 of them full no-op invocations with their own
# activity events (review finding). This claim is atomic, so exactly one shard
# publishes per window.
BACKFILL_TRIGGER_CLAIM_SOURCE = "backfill_trigger_claim"
# Head fetches re-cover this much of the cursor's trailing edge: rows whose
# server-assigned UploadTimeStamp falls inside a window we already queried can
# become queryable only after our request completed (write latency / clock
# skew), and the gap never re-opens to catch them. Re-sends are tolerated
# downstream, so the overlap is cheap insurance (carried over from the
# pre-5602 cursor, which used the same 2h buffer).
HEAD_LATE_UPLOAD_OVERLAP = timedelta(hours=2)


def _deadline_exceeded(run_started_at):
    elapsed = (datetime.now(tz=timezone.utc) - run_started_at).total_seconds()
    return elapsed > DEADLINE_FRACTION * app_settings.MAX_ACTION_EXECUTION_TIME


def _slot_wait_budget(run_started_at):
    """Seconds this run can still afford to spend queueing for a connection
    slot. Mirrors _deadline_exceeded's fraction so waiting stops exactly when
    the traversal would have stopped anyway."""
    elapsed = (datetime.now(tz=timezone.utc) - run_started_at).total_seconds()
    return max(0.0, DEADLINE_FRACTION * app_settings.MAX_ACTION_EXECUTION_TIME - elapsed)


def _fetch_retry_kwargs(run_started_at):
    # Past the soft deadline, don't spend the remaining budget re-trying slow
    # transport failures — but keep the token-expiry retry: it is a cheap
    # re-auth, and dropping it breaks the "token expiry is retried and
    # recovers within the run" contract (review finding).
    if _deadline_exceeded(run_started_at):
        return {"on": LotekTokenExpiredException, "attempts": RETRY_ATTEMPTS}
    return {"on": RETRYABLE_ERRORS, "attempts": RETRY_ATTEMPTS}


class RunGuards:
    """Per-run deadline + circuit-breaker state for a device loop."""

    def __init__(self, run_started_at):
        self.run_started_at = run_started_at
        self.consecutive_transport_failures = 0

    def should_stop(self):
        if _deadline_exceeded(self.run_started_at):
            return "deadline"
        if self.consecutive_transport_failures >= BREAKER_THRESHOLD:
            return "circuit breaker"
        return None

    def record(self, transport_failure: bool):
        if transport_failure:
            self.consecutive_transport_failures += 1
        else:
            self.consecutive_transport_failures = 0


def _summarize_ids(ids):
    # A Lotek-wide outage can involve every device; don't put hundreds of ids
    # in a log title.
    listed = ', '.join(ids[:MAX_DEVICES_IN_SUMMARY])
    if len(ids) > MAX_DEVICES_IN_SUMMARY:
        listed += f" and {len(ids) - MAX_DEVICES_IN_SUMMARY} more"
    return listed


async def _log_deferral(integration, action_id, reason, deferred_ids, disposition="to the next run"):
    # disposition tells the operator what actually happens to the tail: a
    # deadline-cut shard hands it to a re-triggered shard immediately, while a
    # breaker stop really does wait for the next scheduled tick (Copilot
    # review: the fixed "to the next run" text was misleading under deadline
    # pressure).
    message = (
        f"Stopping early ({reason}) for integration {integration.id}: deferring "
        f"{len(deferred_ids)} device(s) {disposition}: {_summarize_ids(deferred_ids)}."
    )
    logger.warning(message)
    await log_action_activity(
        integration_id=str(integration.id),
        action_id=action_id,
        title=message,
        level=LogLevel.WARNING
    )


async def _try_log_activity(integration_id, action_id, title, level):
    """Best-effort activity-feed publish for per-device paths. A pubsub blip
    must never escape a per-device handler: an escaping publish discards
    already-delivered counts and resets the circuit-breaker streak with a
    failure that says nothing about Lotek (review finding)."""
    try:
        await log_action_activity(
            integration_id=integration_id,
            action_id=action_id,
            title=title,
            level=level
        )
    except Exception as e:
        logger.warning(
            f"Could not publish activity log for integration {integration_id}: {describe_exception(e)}"
        )


def generate_batches(iterable, n=settings.OBSERVATIONS_BATCH_SIZE):
    for i in range(0, len(iterable), n):
        yield iterable[i: i + n]

def get_auth_config(integration):
    # Look for the login credentials, needed for any action
    auth_config = find_config_for_action(
        configurations=integration.configurations,
        action_id="auth"
    )
    if not auth_config:
        raise ConfigurationNotFound(
            f"Authentication settings for integration {str(integration.id)} "
            f"are missing. Please fix the integration setup in the portal."
        )
    return AuthenticateConfig.parse_obj(auth_config.data)

def get_pull_config(integration):
    # Look for pull observations configuration
    pull_config = find_config_for_action(
        configurations=integration.configurations,
        action_id="pull_observations"
    )
    if not pull_config:
        raise ConfigurationNotFound(
            f"Pull Observations settings for integration {str(integration.id)} "
            f"are missing. Please fix the integration setup in the portal."
        )
    return PullObservationsConfig.parse_obj(pull_config.data)

@action_title("Connect with Lotek")
async def action_auth(integration, action_config: AuthenticateConfig):
    logger.info(f"Executing auth action with integration {integration} and action_config {action_config}...")
    try:
        token = await client.get_token_from_api(integration, action_config)
    except LotekUnauthorizedException as e:
        logger.exception(f"Auth unsuccessful for integration {integration.id}. Exception: {e}")
        return {"valid_credentials": False, "message": "Invalid credentials"}
    except LotekException as e:
        # Login 5xx/429: Lotek is down or throttling, not a credentials problem —
        # don't send the operator off to reset a working password.
        logger.exception(f"Auth action failed for integration {integration.id}. Exception: {e}")
        return {"error": "An internal error occurred while trying to test credentials. Please try again later."}
    except httpx.HTTPError as e:
        logger.exception(f"Auth action failed for integration {integration.id}. Exception: {e}")
        return {"error": "An internal error occurred while trying to test credentials. Please try again later."}
    else:
        if token:
            logger.info(f"Auth successful for integration '{integration.name}'. Token: '{token}'")
            return {"valid_credentials": True}
        else:
            logger.error(f"Auth unsuccessful for integration {integration}.")
            return {"valid_credentials": False}

def filter_and_transform_positions(positions, integration, action_config=None):
    max_pdop = action_config.max_pdop if action_config else None
    filtered_by_pdop = 0
    valid_positions = []
    for position in positions:
        try:
            if position.Longitude is None or position.Latitude is None:
                msg = f"Filtering {position} (bad location) for device {position.DeviceID}."
                logger.info(msg)
                continue

            if max_pdop is not None and position.PDOP > max_pdop:
                logger.debug(
                    f"Filtering position for device {position.DeviceID} "
                    f"(PDOP={position.PDOP} > max_pdop={max_pdop})."
                )
                filtered_by_pdop += 1
                continue

            cdip_pos = {
                "source": position.DeviceID,
                "source_name": position.DevName or str(position.DeviceID),
                'type': 'tracking-device',
                "recorded_at": ensure_timezone_aware(position.RecDateTime).isoformat(),
                "location": {
                    "lat": position.Latitude,
                    "lon": position.Longitude,
                    "alt": position.Altitude
                },
                "additional": position.dict(exclude={'DeviceID', 'Latitude', 'Longitude', 'RecDateTime'})
            }
            valid_positions.append(cdip_pos)
        except Exception as ex:
            logger.error(f"Failed to parse Lotek point: {position} for Integration ID {str(integration.id)}. Exception: {ex}")

    if filtered_by_pdop:
        logger.info(f"Filtered {filtered_by_pdop} of {len(positions)} positions by PDOP > {max_pdop}.")

    return valid_positions

def ensure_timezone_aware(val: datetime, default_tz: timezone = timezone.utc) -> datetime:
    # Assume the configured default tz for naive values, then normalize any
    # aware value to UTC so recorded_at is always emitted in UTC.
    if not val.tzinfo:
        val = val.replace(tzinfo=default_tz)
    return val.astimezone(timezone.utc)

@action_title("Integration Settings")
@activity_logger()
async def action_pull_observations(integration, action_config: PullObservationsConfig):
    """Dispatcher (GUNDI-5620, movebank-connector pattern): list the account's
    devices, order them least-fresh first, and fan the fleet out as
    pull_observations_shard sub-actions of SHARD_SIZE ids each. Every shard
    invocation gets its own MAX_ACTION_EXECUTION_TIME budget, so a 600-device
    account no longer has to fit one 540s window.

    RESULT CONTRACT CHANGE (Copilot review): this action no longer does the
    fetching, so it no longer reports `observations_extracted`, `devices_failed`
    or `devices_deferred` — those now belong to each pull_observations_shard
    result (and its completion event). This action returns `devices_found` and
    `shards_triggered`, plus `devices_undispatched` when a publish failed, and
    `skipped`/`reason` when the tick was skipped. Anything aggregating per-tick
    throughput must sum the shard results instead of reading this one.
    """
    # Log only the id: the full Integration object embeds auth config data in
    # plaintext (same leak family as the action-runner _handle_error ticket).
    logger.info(f"Executing pull_observations action for integration {integration.id}...")

    integration_id = str(integration.id)
    auth = get_auth_config(integration)
    try:
        async for attempt in stamina.retry_context(on=RETRYABLE_ERRORS, attempts=RETRY_ATTEMPTS, wait_initial=RETRY_WAIT_INITIAL, wait_jitter=RETRY_WAIT_JITTER, wait_max=RETRY_WAIT_MAX):
            with attempt:
                # Token outside the slot — see _fetch_window.
                await client.get_token(integration, auth)
                async with lotek_slot(auth.username):
                    device_list = await client.get_devices(integration, auth)
    except NoConnectionSlot:
        # Account budget saturated (shards/backfills from a previous tick are
        # still draining). Scheduled tick — skip cleanly and let the next one
        # retry, mirroring the movebank pull's no_connection_slot skip. A
        # single skip stays out of the portal, but a STREAK of skipped ticks
        # means the account has been saturated for tens of minutes and must
        # not stay invisible (review finding: this was logger.info only).
        message = (
            f"Skipping pull for integration {integration_id}: Lotek connection budget "
            f"exhausted; the next scheduled tick will retry."
        )
        logger.info(message)
        streak = await _bump_dispatcher_skip_streak(integration_id)
        if streak >= DISPATCHER_SKIP_WARN_AFTER:
            await _try_log_activity(
                integration_id, "pull_observations",
                f"{message} ({streak} consecutive ticks skipped on connection-budget exhaustion.)",
                LogLevel.WARNING,
            )
        return {"skipped": True, "reason": "no_connection_slot"}
    except httpx.TransportError as e:
        # Transport failures reaching Lotek are the same class the per-device
        # breaker treats as WARNING; classifying them ERROR here marked every
        # connection unhealthy during fleet-wide Lotek congestion even though
        # the next tick usually succeeds (GUNDI-5602 prod finding 2026-08-16).
        # Clean return: the run made no progress but the schedule retries it.
        message = (
            f"Lotek API unreachable while listing devices for integration {integration.id}: "
            f"{describe_exception(e)}. The run will be retried on the next schedule."
        )
        logger.warning(message)
        await log_action_activity(
            integration_id=integration_id,
            action_id="pull_observations",
            title=message,
            level=LogLevel.WARNING
        )
        # Any non-starved outcome breaks the "consecutive" skip streak — a
        # transport skip between two slot skips must not let them read as
        # adjacent (review finding).
        await _reset_dispatcher_skip_streak(integration_id)
        return {
            "devices_found": 0,
            "shards_triggered": 0,
            "skipped": True,
            "reason": "lotek_unreachable",
        }
    except Exception as e:
        message = f"Error fetching devices from Lotek. Integration ID: {integration.id} Exception: {describe_exception(e)}"
        logger.exception(message)
        await log_action_activity(
            integration_id=integration_id,
            action_id="pull_observations",
            title=message,
            level=LogLevel.ERROR
        )
        # Same reason as the transport path: a non-starved outcome breaks the
        # "consecutive" skip streak (review finding — this path was missed).
        await _reset_dispatcher_skip_streak(integration_id)
        raise  # bare: preserve the original traceback

    logger.info(f"Extracted {len(device_list)} devices from Lotek for inbound: {integration.id}")
    await _reset_dispatcher_skip_streak(integration_id)
    if not device_list:
        return {"devices_found": 0, "shards_triggered": 0}

    # Least-fresh first (mirrors the backfill's LRS ordering): if shards get
    # cut by their rails, it's always the freshest tail that defers, and the
    # ordering rotates naturally as serviced devices move to the back. Reads
    # only the saved cursor (one Redis get per device); devices with no state
    # yet sort first (most behind by definition).
    epoch = datetime.min.replace(tzinfo=timezone.utc)

    async def read_high_water(device_id):
        # quiet: the shard publishes the unparseable-state warning when it
        # actually discards the cursor; this read only orders the fleet.
        state = await _read_device_state(integration_id, device_id, quiet=True)
        return device_id, state.high_water if state else epoch

    # Bounded-concurrency reads (Copilot review): sequential awaits made this
    # sort a per-device Redis round trip — a latency multiplier on exactly the
    # large fleets sharding exists to serve. STATE_READ_CONCURRENCY keeps the
    # dispatcher fast without a thundering herd on Redis.
    staleness = []
    all_ids = [device.nDeviceID for device in device_list]
    for batch in generate_batches(all_ids, STATE_READ_CONCURRENCY):
        staleness.extend(await asyncio.gather(*(read_high_water(d) for d in batch)))
    staleness.sort(key=lambda entry: entry[1])
    device_ids = [device_id for device_id, _ in staleness]

    # Manual-run marker: the runner's pause check only skips SCHEDULED runs,
    # so if this dispatcher is executing while run_on_schedule is off, the run
    # is manual — the shards it publishes are machine-triggered and would
    # otherwise skip on the pause, silently breaking the portal's Trigger
    # button for the whole head pass (review finding).
    manual_run = not action_config.run_on_schedule

    # Per-shard guard (review finding): an unguarded loop let one pubsub blip
    # abort every remaining shard AND escape through the runner's generic
    # error handler (whose ERROR event embeds the integration's full config,
    # auth included). Failures are collected, not fatal — the next tick
    # re-lists and re-shards everything.
    shards = list(generate_batches(device_ids, SHARD_SIZE))
    dispatched = 0
    undispatched_devices = []
    for shard in shards:
        try:
            await trigger_action(
                integration_id, "pull_observations_shard",
                config=PullObservationsShardConfig(
                    devices=shard, triggered_by="pull_observations", manual_run=manual_run
                )
            )
            dispatched += 1
        except Exception as e:
            undispatched_devices.extend(shard)
            logger.warning(
                f"Could not dispatch a shard of {len(shard)} device(s) for integration "
                f"{integration_id}: {describe_exception(e)}"
            )
    if undispatched_devices:
        message = (
            f"Dispatched {dispatched} of {len(shards)} shard(s) for integration "
            f"{integration_id}; {len(undispatched_devices)} device(s) get no pull this "
            f"tick and are retried on the next schedule: {_summarize_ids(undispatched_devices)}."
        )
        logger.warning(message)
        await _try_log_activity(integration_id, "pull_observations", message, LogLevel.WARNING)
    if shards and dispatched == 0:
        # Nothing was handed off at all: systemic (commands topic down). ERROR
        # activity event rather than a raise, for the same reason as the shard's
        # zero-progress path — a raise routes through the runner's generic
        # _handle_error, which publishes every integration configuration
        # (plaintext Lotek password included) into the event payload.
        message = (
            f"Could not dispatch any of {len(shards)} shard(s) for integration "
            f"{integration_id}; see the application log for the publish errors."
        )
        logger.error(message)
        await _try_log_activity(integration_id, "pull_observations", message, LogLevel.ERROR)
    result = {"devices_found": len(device_list), "shards_triggered": dispatched}
    if undispatched_devices:
        result["devices_undispatched"] = undispatched_devices
    return result


async def _bump_dispatcher_skip_streak(integration_id):
    """Count consecutive dispatcher runs that pulled nothing because the account
    connection budget was saturated. Atomic INCR: two dispatcher runs in the
    same window (schedule tick plus a redelivery or manual trigger) both
    starving would lose an increment under a client-side read-modify-write, and
    the counter would silently never reach DISPATCHER_SKIP_WARN_AFTER."""
    try:
        return await state_manager.increment_counter(
            integration_id, "pull_observations",
            source_id=DISPATCHER_SKIP_STREAK_SOURCE,
            ttl_seconds=24 * 3600,
        )
    except Exception as e:
        logger.warning(
            f"Could not track skip streak for integration {integration_id}: {describe_exception(e)}"
        )
        return 1


async def _reset_dispatcher_skip_streak(integration_id):
    try:
        await state_manager.delete_state(
            integration_id, "pull_observations", DISPATCHER_SKIP_STREAK_SOURCE
        )
    except Exception as e:
        logger.warning(
            f"Could not reset skip streak for integration {integration_id}: {describe_exception(e)}"
        )


async def _retrigger_shard(integration, device_ids, generation, manual_run=False):
    """Re-dispatch deferred devices as a fresh shard with its own budget,
    instead of parking them until the next scheduled tick.

    Governed by SHARD_RETRIGGER_CAP: `generation` is the CURRENT shard's hop
    count, and the cap bounds how deep a defer-retrigger chain can grow before
    the tail falls back to the next scheduled tick — an ungoverned chain under
    sustained slot starvation was an unbounded busy-loop of zero-progress
    invocations, invisible because each hop reported success (review finding).

    Returns one of:
      RETRIGGER_HANDED_OFF  — a fresh shard owns the tail
      RETRIGGER_CAP_REACHED — cap hit; the tail waits for the next tick and the
                              exhaustion was already reported at ERROR, so the
                              caller must NOT also emit its own alert (review
                              finding: one load event produced two ERRORs)
      RETRIGGER_FAILED      — publish failed; nobody owns the tail this tick
    """
    integration_id = str(integration.id)
    if generation >= SHARD_RETRIGGER_CAP:
        # ERROR, not WARNING: the account failed to drain within ~cap action
        # budgets — either the connection budget is far too small for the
        # fleet or something is systemically slow. The data self-heals on the
        # next tick; the signal must not.
        message = (
            f"Shard re-trigger cap ({SHARD_RETRIGGER_CAP}) reached for integration "
            f"{integration_id}: deferring {len(device_ids)} device(s) to the next "
            f"scheduled tick: {_summarize_ids(device_ids)}."
        )
        logger.error(message)
        await _try_log_activity(
            integration_id, "pull_observations_shard", message, LogLevel.ERROR
        )
        return RETRIGGER_CAP_REACHED
    try:
        await trigger_action(
            integration_id, "pull_observations_shard",
            config=PullObservationsShardConfig(
                devices=device_ids,
                triggered_by="pull_observations_shard",
                generation=generation + 1,
                manual_run=manual_run,
            )
        )
        return RETRIGGER_HANDED_OFF
    except Exception as e:
        logger.warning(
            f"Could not re-trigger shard for integration {integration_id}: {describe_exception(e)}"
        )
        return RETRIGGER_FAILED


# on_start=False: the dispatcher's own started event already marks the tick, and
# a started event per shard doubled the publish volume on the aiohttp/pubsub
# path GUNDI-5620 identified as the dominant error source (review finding). The
# completion event is kept — it carries this shard's observations_extracted.
@activity_logger(on_start=False)
async def action_pull_observations_shard(integration, action_config: PullObservationsShardConfig):
    """Internal action: process one shard of the head pass. Machine-triggered
    by pull_observations (and by itself, for a deadline-deferred tail)."""
    logger.info(
        f"Executing pull_observations_shard ({len(action_config.devices)} devices) "
        f"for integration {integration.id}..."
    )
    integration_id = str(integration.id)
    run_started = datetime.now(tz=timezone.utc)
    try:
        auth = get_auth_config(integration)
        pull_config = get_pull_config(integration)
    except ConfigurationNotFound as e:
        # Skip quietly, mirroring backfill: machine-triggered, so raising would
        # route through the generic _handle_error — an ERROR event embedding
        # the integration's full config (auth included). The scheduled parent
        # is where a missing config gets surfaced.
        logger.warning(
            f"Skipping shard for integration {integration_id}: {describe_exception(e)}"
        )
        return {"skipped": True, "reason": "configuration_missing"}

    if not pull_config.run_on_schedule and not action_config.manual_run:
        # The operator's pause toggle must also stop the shard cascade:
        # internal actions bypass the runner's skippable_pull pause check.
        # manual_run exempts shards belonging to a portal-triggered run — the
        # runner only pauses SCHEDULED runs, and without the exemption a
        # manual Trigger on a paused integration dispatched shards that all
        # skipped, showing success while pulling nothing (review finding).
        logger.info(f"Integration {integration_id} is paused (run_on_schedule=false); skipping shard.")
        return {"skipped": True, "reason": "integration_paused"}

    present_time = datetime.now(tz=timezone.utc)
    device_states = []
    for device_id in action_config.devices:
        state, is_new = await _load_device_state(
            integration_id, device_id, present_time, pull_config
        )
        device_states.append((device_id, state, is_new))
    # Least-fresh first within the shard too: a rail cut defers the freshest tail.
    device_states.sort(key=lambda entry: entry[1].high_water)

    guards = RunGuards(run_started)
    traversal = DeviceTraversal(
        integration, "pull_observations_shard", guards, concurrency=FETCH_CONCURRENCY
    )
    observations_extracted = 0
    stale_drops = []
    # Only reflects devices actually processed this run — a device deferred by
    # the rails before its gap status is checked doesn't trigger backfill this
    # cycle. Self-correcting: the re-triggered tail (or the next tick) reaches it.
    any_open_gap = False

    async for (device_id, state, is_new), res in traversal.run(
        device_states,
        key=lambda entry: entry[0],
        process=lambda entry: _head_pass_device(
            entry[0], entry[1], entry[2], integration, auth, pull_config,
            present_time, guards, stale_drops=stale_drops,
        ),
    ):
        sent, device_failed, transport_failure = res
        # Single recording site: transport failures arm the breaker, anything
        # else (success included) breaks the consecutive streak.
        guards.record(transport_failure=transport_failure)
        observations_extracted += sent
        if state.has_gap:
            any_open_gap = True
        if device_failed:
            traversal.mark_failed(device_id)

    failed_devices = traversal.failed_devices
    deferred_devices = traversal.deferred_devices

    # Policy, deliberately NOT in the traversal (spec D6): a deadline cut gets a
    # fresh budget immediately; a hot breaker does NOT re-trigger — that would
    # defeat the pause the breaker exists to buy. Its devices wait for the next
    # scheduled tick.
    retrigger_outcome = None
    if traversal.stop_reason == "deadline" and deferred_devices:
        retrigger_outcome = await _retrigger_shard(
            integration, deferred_devices, action_config.generation,
            manual_run=action_config.manual_run,
        )
    if traversal.stop_reason:
        await _log_deferral(
            integration, "pull_observations_shard", traversal.stop_reason,
            deferred_devices,
            disposition=(
                "to an immediately re-triggered shard"
                if retrigger_outcome == RETRIGGER_HANDED_OFF
                else "to the next scheduled run"
            ),
        )
    if traversal.budget_starved:
        # Portal WARNING like every other deferral cause: a starved tail must
        # not park with zero portal visibility (review finding).
        await _log_deferral(
            integration, "pull_observations_shard", "connection budget exhausted",
            deferred_devices, disposition="to the next scheduled run",
        )

    if failed_devices:
        message = (
            f"Pulled observations with {len(failed_devices)} of {len(action_config.devices)} device(s) "
            f"failing for integration {integration.id}: {_summarize_ids(failed_devices)}. "
            f"They will be retried on the next run."
        )
        logger.warning(message)
        await log_action_activity(
            integration_id=integration_id,
            action_id="pull_observations_shard",
            title=message,
            level=LogLevel.WARNING
        )

    if stale_drops:
        # Permanent data loss must stay visible in the portal: one aggregated
        # WARNING per run instead of the old per-device publish (review
        # finding on the publish-volume fix). Per-device ranges are in the
        # local log.
        message = (
            f"Dropped data older than max_data_age_hours={pull_config.max_data_age_hours} "
            f"permanently for {len(stale_drops)} device(s) on integration {integration.id}: "
            f"{_summarize_ids(stale_drops)}. See the application log for per-device ranges."
        )
        logger.warning(message)
        await log_action_activity(
            integration_id=integration_id,
            action_id="pull_observations_shard",
            title=message,
            level=LogLevel.WARNING
        )

    # Suppression policy, preserved EXACTLY as it was before the traversal
    # existed: a successful hand-off, a cap-reached deferral (which already
    # alerted at ERROR inside _retrigger_shard), or slot starvation all explain
    # the lack of progress. A BREAKER stop deliberately does not — a Lotek-wide
    # outage must still raise the zero-progress ERROR that the cdip health
    # metric counts.
    deferred_cleanly = (
        retrigger_outcome in (RETRIGGER_HANDED_OFF, RETRIGGER_CAP_REACHED)
        or traversal.budget_starved
    )
    zero_progress = (
        device_states and traversal.serviced_devices == 0
        and observations_extracted == 0 and not deferred_cleanly
    )
    if zero_progress:
        # Zero progress: nothing serviced, nothing delivered, and no deferred
        # tail re-dispatched — systemic degradation that must alert rather than
        # pass as a clean completion. A successfully re-triggered deferral is
        # progress (the work moved to a fresh budget); slot starvation and a
        # cap-reached deferral are clean back-offs that alert on their own
        # branches.
        #
        # Reported as an ERROR activity event, NOT raised (review finding):
        # raising routes through the runner's generic _handle_error, which
        # publishes `config_data` containing every integration configuration —
        # the auth action's plaintext Lotek password included — and a fleet-wide
        # outage would leak it once per shard, up to SHARD_SIZE-many times per
        # tick. An ERROR activity event carries the same health signal (the
        # unhealthy-connection metric counts ERROR activity events) without the
        # credential exposure.
        message = (
            f"No devices could be serviced for integration {integration.id}: "
            f"{len(failed_devices)} failed, {len(deferred_devices)} deferred of "
            f"{len(action_config.devices)}. See the per-device errors in this action's activity log."
        )
        logger.error(message)
        await _try_log_activity(
            integration_id, "pull_observations_shard", message, LogLevel.ERROR
        )

    if any_open_gap and not zero_progress:
        claimed = False
        published = False
        try:
            # Atomic per-window claim first: concurrent shards all reach this
            # point within seconds of each other, and the lease below is only
            # created a pubsub hop later (when the backfill handler runs), so a
            # plain lease read let every shard publish its own command (review
            # finding). Exactly one shard wins the claim per window.
            claimed = await state_manager.set_if_absent(
                integration_id, "backfill_observations",
                ttl_seconds=app_settings.MAX_ACTION_EXECUTION_TIME,
                source_id=BACKFILL_TRIGGER_CLAIM_SOURCE,
            )
            lease = await state_manager.get_state(
                integration_id, "backfill_observations", BACKFILL_LEASE_SOURCE
            )
            if claimed and not lease:
                # backfill_observations is an InternalActionConfiguration with no
                # persisted portal config, so this MUST carry a non-empty config
                # override — a bare trigger_action(..., "backfill_observations")
                # publishes an empty config_overrides, which execute_action reads
                # as "no config at all" and 404s before the handler ever runs.
                await trigger_action(
                    integration_id, "backfill_observations",
                    config=BackfillObservationsConfig(
                        triggered_by="pull_observations_shard",
                        # Carry the manual marker: without it a Trigger on a
                        # paused integration pulled head data but its backfill
                        # skipped on the pause, silently importing no history
                        # (review finding).
                        manual_run=action_config.manual_run,
                    )
                )
                published = True
        except Exception as e:
            # The shard succeeded; a failed trigger must not fail the run.
            logger.warning(
                f"Could not trigger backfill for integration {integration.id}: {describe_exception(e)}"
            )
        finally:
            if claimed and not published:
                # We consumed the per-window claim but never published, so no
                # backfill command exists. Leaving the claim would suppress
                # every other shard this tick AND the next (TTL is a full
                # action budget); pre-sharding a lost trigger self-healed on
                # the next run. Give the claim back (review finding).
                try:
                    await state_manager.delete_state(
                        integration_id, "backfill_observations",
                        source_id=BACKFILL_TRIGGER_CLAIM_SOURCE,
                    )
                except Exception as e:
                    logger.warning(
                        f"Could not release the backfill trigger claim for integration "
                        f"{integration.id} (the TTL will expire it): {describe_exception(e)}"
                    )

    result = {
        'observations_extracted': observations_extracted,
        'devices_failed': failed_devices,
        'devices_deferred': deferred_devices,
    }
    if zero_progress:
        # Only present on the bad path (like `skipped`/`reason` elsewhere), so
        # the systemic-degradation signal is machine-readable in the completion
        # event without changing the shape of every healthy result.
        result['zero_progress'] = True
    return result


async def _read_device_state(integration_id, device_id, action_id="pull_observations", quiet=False):
    """Read one device's saved state. Returns a DeviceState, or None when the
    key is absent or unparseable. Unparseable state is surfaced at WARNING —
    it means a cursor is about to be discarded and the lookback re-imported
    (review finding: this was announced only at DEBUG).

    `quiet` suppresses the portal publish for readers that are not the writer:
    the dispatcher reads every device's cursor just to order the fleet, and the
    shard that actually discards the cursor publishes the same warning moments
    later — publishing on both doubled the volume per affected device (review
    finding). The local log line is kept either way."""
    saved = await state_manager.get_state(integration_id, "pull_observations", device_id)
    if not saved:
        return None
    try:
        return DeviceState.parse_obj(saved)
    except pydantic.ValidationError as e:
        message = (
            f"Discarding unparseable saved state for device {device_id}: the cursor "
            f"resets and the lookback window will re-import. Integration ID: "
            f"{integration_id} Error: {describe_exception(e)}"
        )
        logger.warning(message)
        if not quiet:
            await _try_log_activity(integration_id, action_id, message, LogLevel.WARNING)
        return None


async def _load_device_state(integration_id, device_id, present_time, action_config):
    """Returns (state, is_new). is_new means no usable saved state existed and
    the returned state is the first-run initialization (gap birth)."""
    state = await _read_device_state(integration_id, device_id)
    head_start = present_time - timedelta(hours=action_config.max_data_age_hours)
    if state is not None:
        if state.migrated_from_legacy and state.high_water < head_start and not state.has_gap:
            # Upgrade path: a pre-5602 cursor lagging beyond the freshness
            # floor still owes [cursor, floor] — the old walk would have
            # caught it up in chunks. Carry it over as the device's gap
            # (backfill drains it) instead of letting bounded staleness drop
            # it; is_new=True so the head pass persists the full document.
            state.gap_start = state.high_water
            state.gap_end = head_start
            state.high_water = head_start
            return state, True
        return state, False
    # First run: the head pass starts at the freshness floor; everything older,
    # back to the configured lookback, becomes the device's one and only gap —
    # the deliberate historical import. It only ever shrinks from here.
    gap_start = present_time - timedelta(days=action_config.default_lookback_days)
    if gap_start < head_start:
        return DeviceState(high_water=head_start, gap_start=gap_start, gap_end=head_start), True
    return DeviceState(high_water=head_start), True


async def _save_device_state_fields(integration_id, device_id, updates, init_only=None):
    """Merge-save: atomically overwrite only the fields this writer owns
    (head pass: high_water; backfill: gap_*/last_backfilled).

    The two actions can interleave — the Redis lease only serializes backfill
    against backfill — and whole-blob writes from a stale snapshot were
    resurrecting closed gaps and rewinding the head cursor (review finding:
    lost-update race). The merge runs server-side in a Lua script, so there
    is no read-to-write window at all (review finding: the previous
    client-side read-merge-write only narrowed the race).

    `init_only` is the create-only channel for gap birth: those fields are
    written only when the stored document lacks the key entirely, so a stale
    first-run/migration snapshot cannot overwrite gap progress a backfill
    already made (a closed gap stores the key as null — still present).
    """
    await state_manager.merge_state_fields(
        integration_id, "pull_observations", updates, device_id, init_only=init_only
    )


async def _fetch_window(device_id, integration, auth, config, lower_date, upper_date, guards, action_id):
    """Fetch + transform one window for one device.

    Returns (cdip_positions | None, transport_failure). None means the fetch
    failed — already logged and classified; the caller marks the device failed
    and records transport_failure for the circuit breaker. Raises only
    LotekUnauthorizedException (integration-wide) and NoConnectionSlot
    (account-wide budget, the caller defers and re-triggers).
    """
    integration_id = str(integration.id)
    try:
        async for attempt in stamina.retry_context(**_fetch_retry_kwargs(guards.run_started_at), wait_initial=RETRY_WAIT_INITIAL, wait_jitter=RETRY_WAIT_JITTER, wait_max=RETRY_WAIT_MAX):
            with attempt:
                # Pre-resolve the token OUTSIDE the slot: get_positions calls
                # get_token internally, and on a cache miss that is a full
                # login (plus possible queueing on the per-integration token
                # lock) — holding a slot through it both starves peers and
                # risks outliving the slot TTL (review finding). Pre-warmed,
                # the in-slot get_token is a single cached Redis read.
                await client.get_token(integration, auth)
                # The slot is held for exactly one request and re-acquired on
                # retry, so a stamina backoff never parks a slot idle.
                async with lotek_slot(
                    auth.username,
                    max_wait_seconds=_slot_wait_budget(guards.run_started_at),
                ):
                    positions = await client.get_positions(device_id, auth, integration, lower_date, upper_date, True)
        logger.info(f"Extracted {len(positions)} obs from Lotek for device: {device_id} between {lower_date} and {upper_date}.")
        # Transform inside the try: a malformed payload is a per-device, fetch-class
        # failure and must get the same device/date-window log and isolation.
        return filter_and_transform_positions(positions, integration, config), False
    except LotekUnauthorizedException:
        # Credentials are an integration-wide problem: every remaining device
        # would fail the same way, so fail fast instead of N identical errors.
        raise
    except NoConnectionSlot:
        # Account-wide connection budget exhausted (other shards/backfills are
        # saturating it). Not a device failure and not evidence about Lotek —
        # the caller defers the rest of its devices and re-triggers.
        raise
    except httpx.TransportError as e:
        # WARNING + breaker-feeding: enough timeouts/transport failures in a
        # row mean Lotek-wide degradation, not a bad device.
        # Local log only: failed devices are aggregated into one end-of-run
        # summary publish. Per-device publishes multiplied exactly when Lotek
        # (or the instance) was already drowning — a congestion feedback loop.
        message = f"Error fetching positions from Lotek. Device: {device_id}. Dates: [{lower_date},{upper_date}]. Integration ID: {integration_id} Exception: {describe_exception(e)}"
        logger.warning(message, exc_info=True)
        return None, True
    except LotekException as e:
        message = f"Error fetching positions from Lotek. Device: {device_id}. Dates: [{lower_date},{upper_date}]. Integration ID: {integration_id} Exception: {describe_exception(e)}"
        if e.status_code >= 500 or e.status_code == 429:
            # A Lotek-side 5xx/429 is outage/throttling, the same class of
            # Lotek-wide degradation as a timeout: WARNING + breaker-feeding.
            # Before this branch it fell into the generic handler below, which
            # actively RESET the breaker streak — an HTTP-error outage could
            # never trip the breaker (review finding). Local log only — same
            # aggregation rationale as the transport branch above.
            logger.warning(message, exc_info=True)
            return None, True
        # Other 4xx (incl. a token-expiry 401 that survived its retry): a
        # per-device/API-contract problem that won't self-heal — ERROR, and
        # not breaker-feeding.
        logger.exception(message)
        await _try_log_activity(integration_id, action_id, message, LogLevel.ERROR)
        return None, False
    except Exception as e:
        # Deliberately broad: malformed data from Lotek surfaces as KeyError or
        # pydantic.ValidationError out of the client's parsing, and those must
        # not take down the devices behind this one either. CancelledError is a
        # BaseException, so the action timeout still unwinds normally.
        # ERROR, not WARNING: unlike a transient timeout (httpx.TransportError,
        # above), a data-shape break is permanent and won't self-heal on retry —
        # it must stay visible to health/alerting or it can persist unnoticed
        # forever (review finding).
        message = f"Error fetching positions from Lotek. Device: {device_id}. Dates: [{lower_date},{upper_date}]. Integration ID: {integration_id} Exception: {describe_exception(e)}"
        logger.exception(message)
        await _try_log_activity(integration_id, action_id, message, LogLevel.ERROR)
        return None, False


async def _deliver(cdip_positions, device_id, integration, action_id):
    """Send one device's transformed positions to Gundi in batches.

    Returns (observations_sent, delivery_failed). Handled here rather than in
    the caller so batches already delivered keep counting toward the run's
    total — otherwise a send failure after a successful delivery reports
    "nothing delivered". The cursor stays untouched on failure, so the
    un-delivered remainder is re-fetched next run (re-sends are tolerated,
    silent skips are not).
    """
    integration_id = str(integration.id)
    observations_sent = 0
    try:
        if cdip_positions:
            logger.info(f"{len(cdip_positions)} observations pulled successfully for device {device_id} integration ID: {integration.id}.")
            for i, batch in enumerate(generate_batches(cdip_positions)):
                logger.info(f'Sending observations batch #{i}: {len(batch)} observations. Device: {device_id}')
                await gundi_tools.send_observations_to_gundi(observations=batch, integration_id=integration.id)
                observations_sent += len(batch)
    except Exception as e:
        message = (
            f"Error delivering observations for device {device_id}. Integration ID: "
            f"{integration.id} Exception: {describe_exception(e)}"
        )
        # needs_attention drives log-based alerting on delivery failures (template
        # convention) — kept from the pre-isolation send-loop handler.
        logger.exception(message, extra={
            'needs_attention': True,
            'integration_id': integration_id,
            'action_id': action_id
        })
        # Guarded: an escaping publish here would discard observations_sent —
        # batches that DID land — and could flip a partially-delivered run
        # into the zero-progress ERROR raise (review finding).
        await _try_log_activity(integration_id, action_id, message, LogLevel.ERROR)
        return observations_sent, True
    return observations_sent, False


async def _head_pass_device(device_id, state, is_new, integration, auth, action_config, present_time, guards, stale_drops=None):
    """Fetch, deliver and checkpoint one device's freshest window.

    Returns (observations_sent, device_failed, transport_failure).
    Raises only for integration-wide problems; per-device problems are
    reported through the returned flags so the caller can keep going (and
    record transport_failure for the circuit breaker in one place).
    Fetch-phase transport failures are WARNINGs (transient while Lotek is
    slow; devices_failed tracks them); data-shape, delivery and checkpoint
    failures stay ERRORs.
    """
    integration_id = str(integration.id)
    freshness_floor = present_time - timedelta(hours=action_config.max_data_age_hours)
    # Bounded staleness (GUNDI-5602, deliberate): anything the cursor still
    # owes beyond max_data_age_hours is dropped permanently — never added to
    # the gap — so catch-up cost cannot compound. The drop only becomes real
    # when the cursor actually advances (successful save below), so the
    # WARNING is deferred until then: announcing it before the fetch
    # misreported still-recoverable data as dropped on every failed attempt
    # (review finding).
    stale_from = state.high_water if state.high_water < freshness_floor else None

    lower_date = max(state.high_water - HEAD_LATE_UPLOAD_OVERLAP, freshness_floor)
    cdip_positions, transport_failure = await _fetch_window(
        device_id, integration, auth, action_config, lower_date, present_time, guards, "pull_observations_shard"
    )
    if cdip_positions is None:
        return 0, True, transport_failure

    if not cdip_positions:
        # Local log only. This used to publish a portal WARNING per quiet
        # device — on a mostly-dormant 400-device integration that was
        # hundreds of pubsub publishes per tick, the single largest
        # contributor to the publish congestion behind GUNDI-5602.
        logger.info(f"No positions fetched for device {device_id} integration ID: {integration.id}.")

    observations_sent, delivery_failed = await _deliver(cdip_positions, device_id, integration, "pull_observations_shard")
    if delivery_failed:
        # The breaker watches Lotek, not Gundi: a delivery failure is not
        # evidence of Lotek-wide degradation.
        return observations_sent, True, False

    # Advance the cursor to the queried upper bound (upload time), not recorded_at.
    # Queries are by upload date, so wall clock is the correct cursor, and it must
    # advance even when a device returns no positions. On failure the cursor stays
    # untouched so the head window is re-fetched next run (re-sends are tolerated,
    # silent skips are not).
    state.high_water = present_time
    # The head pass owns only high_water; a first run / legacy migration also
    # births the gap, but create-only — two head runs can overlap, and if the
    # faster one already birthed the document and a backfill advanced or
    # closed the gap, this run's stale snapshot must not resurrect it (review
    # finding). Everything else belongs to the backfill (merge-save, see
    # _save_device_state_fields); last_backfilled is deliberately never
    # written here.
    updates = {"high_water": state.high_water}
    init_only = None
    if is_new:
        updates["version"] = state.version
        init_only = {"gap_start": state.gap_start, "gap_end": state.gap_end}
    try:
        await _save_device_state_fields(integration_id, device_id, updates, init_only=init_only)
    except Exception as e:
        message = (
            f"Error saving cursor for device {device_id}. Integration ID: "
            f"{integration.id} Exception: {describe_exception(e)}"
        )
        logger.exception(message)
        await _try_log_activity(integration_id, "pull_observations_shard", message, LogLevel.ERROR)
        return observations_sent, True, False

    if stale_from is not None:
        # Only now is the drop real: the cursor advanced past the owed range.
        # Per-device detail is local-log only (migration/catch-up days fire
        # this for hundreds of devices in one run), but a permanent drop must
        # not vanish from the portal — the caller aggregates stale_drops into
        # one end-of-run summary publish (review finding).
        logger.warning(
            f"Dropped stale range [{stale_from.isoformat()}, {freshness_floor.isoformat()}] "
            f"permanently for device {device_id}: older than max_data_age_hours="
            f"{action_config.max_data_age_hours}. Integration ID: {integration_id}"
        )
        if stale_drops is not None:
            stale_drops.append(device_id)

    return observations_sent, False, False


async def _backfill_device(device, state, integration, auth, pull_config, guards):
    """Close up to BACKFILL_MAX_WINDOWS_PER_DEVICE oldest windows of one
    device's gap.

    Returns (observations_sent, device_failed, transport_failure, gap_closed,
    windows_advanced). gap_start advances only past windows that were actually
    delivered, so a failure re-fetches the same window next trigger (re-sends
    are tolerated, silent skips are not). windows_advanced counts those
    actually-delivered windows: the caller only keeps the self-retrigger
    cascade going while some gap is really shrinking.
    """
    integration_id = str(integration.id)
    observations_sent = 0
    device_failed = False
    transport_failure = False
    windows_advanced = 0
    for _ in range(BACKFILL_MAX_WINDOWS_PER_DEVICE):
        if not state.has_gap:
            break
        window_start = state.gap_start
        upper_date = min(state.gap_end, state.gap_start + BACKFILL_WINDOW)

        cdip_positions, transport_failure = await _fetch_window(
            device.nDeviceID, integration, auth, pull_config, window_start, upper_date, guards, "backfill_observations"
        )
        if cdip_positions is None:
            device_failed = True
            break

        sent, delivery_failed = await _deliver(cdip_positions, device.nDeviceID, integration, "backfill_observations")
        observations_sent += sent
        if delivery_failed:
            device_failed = True
            break

        # Advance the gap only past the delivered window; close it when done.
        # The backfill owns only the gap fields (merge-save): a concurrent head
        # pass advancing high_water must survive this write.
        state.gap_start = upper_date
        if not state.has_gap:
            state.gap_start = None
            state.gap_end = None
        await _save_device_state_fields(
            integration_id, device.nDeviceID,
            {"gap_start": state.gap_start, "gap_end": state.gap_end}
        )
        windows_advanced += 1

    # Advances even on zero progress (device_failed on the first window):
    # deliberate LRS-fairness trade-off so one permanently-broken device
    # doesn't monopolize the front of the backfill queue. The zero-progress
    # raise is the actual safety net when it's the only gapped device.
    state.last_backfilled = datetime.now(tz=timezone.utc)
    await _save_device_state_fields(
        integration_id, device.nDeviceID, {"last_backfilled": state.last_backfilled}
    )
    return observations_sent, device_failed, transport_failure, not state.has_gap, windows_advanced


@action_title("Backfill Observations")
@activity_logger()
async def action_backfill_observations(integration, action_config: BackfillObservationsConfig):
    """Internal action: closes per-device historical gaps opened by the head
    pass, oldest-first, least-recently-backfilled device first. Triggered by
    pull_observations; the Redis lease keeps overlapping triggers from
    double-running when a backfill grinds past the next head-pass trigger."""
    # Log only the id: the full Integration object embeds auth config data in
    # plaintext (same leak family as the action-runner _handle_error ticket).
    logger.info(f"Executing backfill_observations action for integration {integration.id}...")
    integration_id = str(integration.id)
    run_started = datetime.now(tz=timezone.utc)
    try:
        auth = get_auth_config(integration)
        pull_config = get_pull_config(integration)  # max_pdop applies to backfilled data too
    except ConfigurationNotFound as e:
        # Skip quietly, mirroring the runner's skippable_pull behavior: backfill
        # is machine-triggered, so raising here would route through the generic
        # _handle_error — an ERROR event embedding the integration's full config
        # (auth included) on every remaining cascade step (review finding). The
        # scheduled head pass is where a missing config gets surfaced.
        logger.warning(
            f"Skipping backfill for integration {integration_id}: {describe_exception(e)}"
        )
        return {"skipped": True, "reason": "configuration_missing"}

    if not pull_config.run_on_schedule and not action_config.manual_run:
        # The operator's pause toggle must also stop the cascade: internal
        # actions bypass the runner's skippable_pull pause check, and backfill
        # is only ever machine-triggered — a paused integration skips cleanly
        # (review finding). manual_run exempts a backfill descending from an
        # operator-triggered head pass: without it a Trigger on a paused
        # integration pulled the head window but silently imported no history
        # (review finding).
        logger.info(f"Integration {integration_id} is paused (run_on_schedule=false); skipping backfill.")
        return {"skipped": True, "reason": "integration_paused"}

    lease_token = await state_manager.acquire_lease(
        integration_id,
        "backfill_observations",
        ttl_seconds=app_settings.MAX_ACTION_EXECUTION_TIME,
        source_id=BACKFILL_LEASE_SOURCE,
    )
    if not lease_token:
        logger.info(f"Backfill lease already held for integration {integration_id}; skipping run.")
        return {"skipped": True, "reason": "lease_held"}

    try:
        try:
            async for attempt in stamina.retry_context(on=RETRYABLE_ERRORS, attempts=RETRY_ATTEMPTS, wait_initial=RETRY_WAIT_INITIAL, wait_jitter=RETRY_WAIT_JITTER, wait_max=RETRY_WAIT_MAX):
                with attempt:
                    # Token outside the slot — see _fetch_window.
                    await client.get_token(integration, auth)
                    async with lotek_slot(
                        auth.username, max_wait_seconds=_slot_wait_budget(run_started)
                    ):
                        device_list = await client.get_devices(integration, auth)
        except NoConnectionSlot:
            # Account budget saturated by head-pass shards: back off quietly.
            # The lease releases via the finally; open gaps re-trigger on the
            # next scheduled head pass.
            logger.info(
                f"Skipping backfill for integration {integration_id}: Lotek connection "
                f"budget exhausted; open gaps are retried on the next trigger."
            )
            return {"skipped": True, "reason": "no_connection_slot"}
        except httpx.TransportError as e:
            # Same WARNING classification as the head pass: an unreachable
            # Lotek must not mark the connection unhealthy. The early return
            # releases the lease via the finally below, and skipping the
            # self-retrigger throttles the cascade to the head-pass cadence —
            # the open gaps re-trigger backfill on the next scheduled run.
            message = (
                f"Lotek API unreachable while listing devices for backfill on integration "
                f"{integration_id}: {describe_exception(e)}. Open gaps are retried on the next trigger."
            )
            logger.warning(message)
            await log_action_activity(
                integration_id=integration_id,
                action_id="backfill_observations",
                title=message,
                level=LogLevel.WARNING
            )
            return {"skipped": True, "reason": "lotek_unreachable"}
        except Exception as e:
            message = (
                f"Error fetching devices from Lotek for backfill. Integration ID: "
                f"{integration_id} Exception: {describe_exception(e)}"
            )
            logger.exception(message)
            await log_action_activity(
                integration_id=integration_id,
                action_id="backfill_observations",
                title=message,
                level=LogLevel.ERROR
            )
            raise  # bare: preserve the original traceback

        gapped = []
        for device in device_list:
            # Same reader as the head pass — absent or unparseable state means
            # the head pass (re-)initializes it; nothing to backfill yet.
            state = await _read_device_state(integration_id, device.nDeviceID, "backfill_observations")
            if state is not None and state.has_gap:
                gapped.append((device, state))
        # Least-recently-backfilled first keeps the import fair; devices never
        # backfilled lead the queue.
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        gapped.sort(key=lambda pair: pair[1].last_backfilled or epoch)

        guards = RunGuards(run_started)
        traversal = DeviceTraversal(
            integration, "backfill_observations", guards, concurrency=FETCH_CONCURRENCY
        )
        observations_extracted = 0
        gaps_closed = 0
        windows_advanced_total = 0

        async for (device, state), res in traversal.run(
            gapped,
            key=lambda pair: pair[0].nDeviceID,
            process=lambda pair: _backfill_device(
                pair[0], pair[1], integration, auth, pull_config, guards
            ),
        ):
            sent, device_failed, transport_failure, gap_closed, windows_advanced = res
            # Single recording site, mirroring the head pass.
            guards.record(transport_failure=transport_failure)
            observations_extracted += sent
            gaps_closed += int(gap_closed)
            windows_advanced_total += windows_advanced
            if device_failed:
                traversal.mark_failed(device.nDeviceID)

        failed_devices = traversal.failed_devices
        deferred_devices = traversal.deferred_devices

        # Policy, deliberately NOT in the traversal (spec D6): unlike the shard,
        # backfill never re-triggers its own tail from here — the cascade below
        # owns that, throttled by gaps_remaining.
        if traversal.stop_reason:
            await _log_deferral(
                integration, "backfill_observations", traversal.stop_reason,
                deferred_devices,
            )
        if traversal.budget_starved:
            await _log_deferral(
                integration, "backfill_observations", "connection budget exhausted",
                deferred_devices, disposition="to the next backfill trigger",
            )

        if failed_devices:
            message = (
                f"Backfilled with {len(failed_devices)} of {len(gapped)} gapped device(s) "
                f"failing for integration {integration_id}: {_summarize_ids(failed_devices)}. "
                f"Their gaps are retried on the next trigger."
            )
            logger.warning(message)
            await log_action_activity(
                integration_id=integration_id,
                action_id="backfill_observations",
                title=message,
                level=LogLevel.WARNING
            )

        # Backfill's suppression policy differs from the shard's and is
        # preserved exactly: ONLY starvation explains a no-progress run here (a
        # budget-starved run is a clean back-off, not degradation). Deadline and
        # breaker stops must still alert.
        zero_progress = (
            gapped and traversal.serviced_devices == 0
            and observations_extracted == 0 and not traversal.budget_starved
        )
        if zero_progress:
            # Same systemic-degradation contract as the head pass. Reported, NOT
            # raised: raising routes through the runner's generic _handle_error,
            # which publishes config_data containing every integration
            # configuration — the auth action's plaintext Lotek password
            # included (GUNDI-5628). An ERROR activity event carries the same
            # health signal without the credential exposure (spec D7).
            message = (
                f"No devices could be backfilled for integration {integration_id}: "
                f"{len(failed_devices)} failed, {len(deferred_devices)} deferred of "
                f"{len(gapped)}. See the per-device errors in this action's activity log."
            )
            logger.error(message)
            await _try_log_activity(
                integration_id, "backfill_observations", message, LogLevel.ERROR
            )

        # _backfill_device mutates the states in place, so this reflects
        # post-run gap status; deferred devices keep their gaps open.
        # A hot breaker suppresses the self-retrigger: re-entering immediately
        # would defeat the pause the breaker exists to buy (review finding) —
        # the next scheduled head pass re-triggers ~cadence later instead.
        # Same for a run that advanced no window at all: with a deterministic
        # per-window failure (e.g. one observation Gundi always rejects),
        # retriggering on has_gap alone spun an unthrottled tight loop that
        # re-fetched and re-sent the same window forever (review finding) —
        # no-progress runs now wait for the next head-pass cadence instead.
        breaker_hot = guards.consecutive_transport_failures >= BREAKER_THRESHOLD
        gaps_remaining = (
            any(state.has_gap for _, state in gapped)
            and not breaker_hot
            and windows_advanced_total > 0
            # Budget starvation suppresses the cascade for the same reason a hot
            # breaker does (Copilot review): a run that advanced a window and
            # then starved would re-trigger straight back into a saturated
            # account budget, competing with the head-pass shards that are
            # holding it. Unlike the shard cascade this one has no generation
            # cap, so the throttle has to come from here; the next scheduled
            # head pass re-triggers it once the budget frees up.
            and not traversal.budget_starved
            # A wholly-failing backfill must not re-trigger itself forever. The
            # removed raise used to guarantee this by unwinding before the
            # re-trigger below; now it has to be explicit (spec D7).
            and not zero_progress
        )
        result = {
            'observations_extracted': observations_extracted,
            'devices_failed': failed_devices,
            'devices_deferred': deferred_devices,
            'gaps_closed': gaps_closed,
        }
        if zero_progress:
            # Only present on the bad path (like `skipped`/`reason` elsewhere),
            # so the systemic-degradation signal is machine-readable in the
            # completion event without changing every healthy result's shape.
            result['zero_progress'] = True
    finally:
        # Ownership-checked release: an unconditional DEL could outlive this
        # run's TTL (e.g. retried through a Redis blip after a cancellation)
        # and delete a successor's lease, allowing overlapping backfills
        # (review finding). Compare-and-delete only removes our own token;
        # if release fails, the TTL expires the lease anyway.
        try:
            await state_manager.release_lease(
                integration_id, "backfill_observations", lease_token,
                source_id=BACKFILL_LEASE_SOURCE,
            )
        except Exception as e:
            logger.warning(
                f"Could not release backfill lease for integration {integration_id} "
                f"(the TTL will expire it): {describe_exception(e)}"
            )

    if gaps_remaining:
        # Self-retrigger (movebank-connector pattern): keep draining the import
        # continuously instead of waiting for the next head-pass tick. Runs
        # AFTER the lease release so the next run doesn't skip on our own lease.
        try:
            await trigger_action(
                integration_id, "backfill_observations",
                config=BackfillObservationsConfig(
                    triggered_by="backfill_observations",
                    manual_run=action_config.manual_run,
                )
            )
        except Exception as e:
            # The next head pass will re-trigger; losing one cascade step is fine.
            logger.warning(
                f"Could not re-trigger backfill for integration {integration_id}: {describe_exception(e)}"
            )
    return result
