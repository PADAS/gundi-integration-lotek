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
from app.actions.core import action_title
from app.actions.device_state import DeviceState
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
# Devices are fetched in bounded-concurrency chunks over the shared HTTP
# client (GUNDI-5620). Sequential fetching put 400-600 Lotek round trips on
# the action budget one at a time, which is what pushed the big integrations
# into the MAX_ACTION_EXECUTION_TIME ceiling. Guards (deadline + breaker) are
# checked between chunks, so their granularity coarsens from 1 device to
# FETCH_CONCURRENCY devices — with the breaker threshold at 3, a fully-bad
# chunk overshoots by at most 2 requests. Chunk results are recorded in list
# order, preserving the sequential consecutive-failure semantics.
FETCH_CONCURRENCY = 5
# Sharded head pass (GUNDI-5620, movebank-connector pattern): the scheduled
# pull_observations only lists devices and dispatches shards of this many
# device ids as pull_observations_shard sub-actions, each with its own action
# budget. Sized so a shard finishes comfortably inside one budget even on a
# slow tick (25 devices / FETCH_CONCURRENCY = 5 chunks of round trips).
SHARD_SIZE = 25
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


async def _log_deferral(integration, action_id, reason, deferred_ids):
    message = (
        f"Stopping early ({reason}) for integration {integration.id}: deferring "
        f"{len(deferred_ids)} device(s) to the next run: {_summarize_ids(deferred_ids)}."
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


def describe_exception(exc):
    # httpx timeout exceptions carry an empty message, which used to render as a bare
    # "Exception: " in the activity log and told operators nothing.
    return str(exc) or type(exc).__name__


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
    account no longer has to fit one 540s window."""
    # Log only the id: the full Integration object embeds auth config data in
    # plaintext (same leak family as the action-runner _handle_error ticket).
    logger.info(f"Executing pull_observations action for integration {integration.id}...")

    integration_id = str(integration.id)
    auth = get_auth_config(integration)
    try:
        async for attempt in stamina.retry_context(on=RETRYABLE_ERRORS, attempts=RETRY_ATTEMPTS, wait_initial=RETRY_WAIT_INITIAL, wait_jitter=RETRY_WAIT_JITTER, wait_max=RETRY_WAIT_MAX):
            with attempt:
                async with lotek_slot(auth.username):
                    device_list = await client.get_devices(integration, auth)
    except NoConnectionSlot:
        # Account budget saturated (shards/backfills from a previous tick are
        # still draining). Scheduled tick — skip cleanly and let the next one
        # retry, mirroring the movebank pull's no_connection_slot skip.
        logger.info(
            f"Skipping pull for integration {integration_id}: Lotek connection budget "
            f"exhausted; the next scheduled tick will retry."
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
        raise  # bare: preserve the original traceback

    logger.info(f"Extracted {len(device_list)} devices from Lotek for inbound: {integration.id}")
    if not device_list:
        return {"devices_found": 0, "shards_triggered": 0}

    # Least-fresh first (mirrors the backfill's LRS ordering): if shards get
    # cut by their rails, it's always the freshest tail that defers, and the
    # ordering rotates naturally as serviced devices move to the back. Reads
    # only the saved cursor (one Redis get per device); devices with no state
    # yet sort first (most behind by definition).
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    staleness = []
    for device in device_list:
        state = await _read_device_state(integration_id, device.nDeviceID)
        staleness.append((device.nDeviceID, state.high_water if state else epoch))
    staleness.sort(key=lambda entry: entry[1])
    device_ids = [device_id for device_id, _ in staleness]

    shards = list(generate_batches(device_ids, SHARD_SIZE))
    for shard in shards:
        await trigger_action(
            integration_id, "pull_observations_shard",
            config=PullObservationsShardConfig(devices=shard, triggered_by="pull_observations")
        )
    return {"devices_found": len(device_list), "shards_triggered": len(shards)}


async def _retrigger_shard(integration_id, device_ids):
    """Re-dispatch deferred devices as a fresh shard with its own budget,
    instead of parking them until the next scheduled tick. Best-effort: the
    next tick re-lists and re-shards everything anyway."""
    try:
        await trigger_action(
            integration_id, "pull_observations_shard",
            config=PullObservationsShardConfig(devices=device_ids, triggered_by="pull_observations_shard")
        )
        return True
    except Exception as e:
        logger.warning(
            f"Could not re-trigger shard for integration {integration_id}: {describe_exception(e)}"
        )
        return False


@activity_logger()
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

    if not pull_config.run_on_schedule:
        # The operator's pause toggle must also stop the shard cascade:
        # internal actions bypass the runner's skippable_pull pause check.
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
    observations_extracted = 0
    failed_devices = []
    deferred_devices = []
    retriggered = False
    serviced_devices = 0
    # Only reflects devices actually processed this run — a device deferred by
    # the rails before its gap status is checked doesn't trigger backfill this
    # cycle. Self-correcting: the re-triggered tail (or the next tick) reaches it.
    any_open_gap = False
    stale_drops = []
    for chunk_start in range(0, len(device_states), FETCH_CONCURRENCY):
        if reason := guards.should_stop():
            deferred_devices = [device_id for device_id, _, _ in device_states[chunk_start:]]
            await _log_deferral(integration, "pull_observations_shard", reason, deferred_devices)
            # A deadline cut gets a fresh budget immediately; a hot breaker
            # does NOT re-trigger — that would defeat the pause the breaker
            # exists to buy. Its devices wait for the next scheduled tick.
            if reason == "deadline":
                retriggered = await _retrigger_shard(integration_id, deferred_devices)
            break
        chunk = device_states[chunk_start:chunk_start + FETCH_CONCURRENCY]
        results = await asyncio.gather(
            *(
                _head_pass_device(
                    device_id, state, is_new, integration, auth, pull_config,
                    present_time, guards, stale_drops=stale_drops
                )
                for device_id, state, is_new in chunk
            ),
            # Collect every task's outcome rather than aborting the chunk on
            # the first exception: per-device failures must stay per-device.
            return_exceptions=True,
        )
        # Credentials refused is integration-wide and fatal; re-raise it over
        # any per-device outcomes in the same chunk. Cancellation must also
        # propagate: with return_exceptions=True a task's CancelledError comes
        # back as a result, and treating it as a device failure would swallow
        # shutdown/timeout cancellation and keep the run going.
        for res in results:
            if isinstance(res, (LotekUnauthorizedException, asyncio.CancelledError)):
                raise res
        slot_starved = []
        for (device_id, state, is_new), res in zip(chunk, results):
            if isinstance(res, NoConnectionSlot):
                # Account connection budget exhausted: not a device failure and
                # not evidence about Lotek — defer this device with the rest of
                # the shard and re-trigger (the pubsub round trip is the backoff,
                # movebank-connector pattern).
                slot_starved.append(device_id)
                continue
            if isinstance(res, BaseException):
                # Sending to Gundi and checkpointing can fail too, and a device that fetched
                # fine but failed downstream must not take the rest of the batch with it.
                message = (
                    f"Failed to process device {device_id} for integration "
                    f"{integration.id}: {describe_exception(res)}"
                )
                logger.error(message, exc_info=res)
                await log_action_activity(
                    integration_id=integration_id,
                    action_id="pull_observations_shard",
                    title=message,
                    level=LogLevel.ERROR
                )
                failed_devices.append(device_id)
                guards.record(transport_failure=False)
                continue
            sent, device_failed, transport_failure = res
            # Single recording site: transport failures arm the breaker, anything
            # else (success included) breaks the consecutive streak.
            guards.record(transport_failure=transport_failure)
            observations_extracted += sent
            if state.has_gap:
                any_open_gap = True
            if device_failed:
                failed_devices.append(device_id)
            else:
                serviced_devices += 1
        if slot_starved:
            deferred_devices = slot_starved + [
                device_id for device_id, _, _ in device_states[chunk_start + FETCH_CONCURRENCY:]
            ]
            logger.info(
                f"Deferring {len(deferred_devices)} device(s) for integration {integration_id}: "
                f"Lotek connection budget exhausted."
            )
            retriggered = await _retrigger_shard(integration_id, deferred_devices)
            break

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

    if device_states and serviced_devices == 0 and observations_extracted == 0 and not retriggered:
        # Zero progress: nothing serviced, nothing delivered, and no deferred
        # tail re-dispatched — systemic degradation, must alert rather than
        # publish action_complete. A successfully re-triggered deferral is
        # progress (the work moved to a fresh budget), so it stays a warning.
        raise LotekException(
            message=(
                f"No devices could be serviced for integration {integration.id}: "
                f"{len(failed_devices)} failed, {len(deferred_devices)} deferred of "
                f"{len(action_config.devices)}. See the per-device errors in this action's activity log."
            )
        )

    if any_open_gap:
        try:
            lease = await state_manager.get_state(
                integration_id, "backfill_observations", BACKFILL_LEASE_SOURCE
            )
            if not lease:
                # backfill_observations is an InternalActionConfiguration with no
                # persisted portal config, so this MUST carry a non-empty config
                # override — a bare trigger_action(..., "backfill_observations")
                # publishes an empty config_overrides, which execute_action reads
                # as "no config at all" and 404s before the handler ever runs.
                await trigger_action(
                    integration_id, "backfill_observations",
                    config=BackfillObservationsConfig(triggered_by="pull_observations_shard")
                )
        except Exception as e:
            # The shard succeeded; a failed trigger must not fail the run.
            logger.warning(
                f"Could not trigger backfill for integration {integration.id}: {describe_exception(e)}"
            )

    return {
        'observations_extracted': observations_extracted,
        'devices_failed': failed_devices,
        'devices_deferred': deferred_devices,
    }


async def _read_device_state(integration_id, device_id, action_id="pull_observations"):
    """Read one device's saved state. Returns a DeviceState, or None when the
    key is absent or unparseable. Unparseable state is surfaced at WARNING —
    it means a cursor is about to be discarded and the lookback re-imported
    (review finding: this was announced only at DEBUG)."""
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
                # The slot is held for exactly one request and re-acquired on
                # retry, so a stamina backoff never parks a slot idle.
                async with lotek_slot(auth.username):
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
        device_id, integration, auth, action_config, lower_date, present_time, guards, "pull_observations"
    )
    if cdip_positions is None:
        return 0, True, transport_failure

    if not cdip_positions:
        # Local log only. This used to publish a portal WARNING per quiet
        # device — on a mostly-dormant 400-device integration that was
        # hundreds of pubsub publishes per tick, the single largest
        # contributor to the publish congestion behind GUNDI-5602.
        logger.info(f"No positions fetched for device {device_id} integration ID: {integration.id}.")

    observations_sent, delivery_failed = await _deliver(cdip_positions, device_id, integration, "pull_observations")
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
        await _try_log_activity(integration_id, "pull_observations", message, LogLevel.ERROR)
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

    if not pull_config.run_on_schedule:
        # The operator's pause toggle must also stop the cascade: internal
        # actions bypass the runner's skippable_pull pause check, and backfill
        # is only ever machine-triggered — a paused integration skips cleanly
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
                    async with lotek_slot(auth.username):
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
        observations_extracted = 0
        failed_devices = []
        deferred_devices = []
        serviced_devices = 0
        gaps_closed = 0
        windows_advanced_total = 0
        budget_starved = False
        for chunk_start in range(0, len(gapped), FETCH_CONCURRENCY):
            if reason := guards.should_stop():
                deferred_devices = [d.nDeviceID for d, _ in gapped[chunk_start:]]
                await _log_deferral(integration, "backfill_observations", reason, deferred_devices)
                break
            chunk = gapped[chunk_start:chunk_start + FETCH_CONCURRENCY]
            results = await asyncio.gather(
                *(
                    _backfill_device(device, state, integration, auth, pull_config, guards)
                    for device, state in chunk
                ),
                # Collect every task's outcome rather than aborting the chunk
                # on the first exception: per-device failures stay per-device.
                return_exceptions=True,
            )
            # Credentials refused is integration-wide and fatal; re-raise it
            # over any per-device outcomes in the same chunk. Cancellation
            # must also propagate (see the head-pass loop).
            for res in results:
                if isinstance(res, (LotekUnauthorizedException, asyncio.CancelledError)):
                    raise res
            slot_starved = []
            for (device, state), res in zip(chunk, results):
                if isinstance(res, NoConnectionSlot):
                    # Account connection budget exhausted (head-pass shards are
                    # saturating it): not a device failure, not evidence about
                    # Lotek. Defer the rest and let the next trigger retry.
                    slot_starved.append(device.nDeviceID)
                    continue
                if isinstance(res, BaseException):
                    message = (
                        f"Failed to backfill device {device.nDeviceID} for integration "
                        f"{integration_id}: {describe_exception(res)}"
                    )
                    logger.error(message, exc_info=res)
                    await log_action_activity(
                        integration_id=integration_id,
                        action_id="backfill_observations",
                        title=message,
                        level=LogLevel.ERROR
                    )
                    failed_devices.append(device.nDeviceID)
                    guards.record(transport_failure=False)
                    continue
                sent, device_failed, transport_failure, gap_closed, windows_advanced = res
                # Single recording site, mirroring the head-pass loop.
                guards.record(transport_failure=transport_failure)
                observations_extracted += sent
                gaps_closed += int(gap_closed)
                windows_advanced_total += windows_advanced
                if device_failed:
                    failed_devices.append(device.nDeviceID)
                else:
                    serviced_devices += 1
            if slot_starved:
                deferred_devices = slot_starved + [
                    d.nDeviceID for d, _ in gapped[chunk_start + FETCH_CONCURRENCY:]
                ]
                budget_starved = True
                logger.info(
                    f"Deferring {len(deferred_devices)} gapped device(s) for integration "
                    f"{integration_id}: Lotek connection budget exhausted."
                )
                break

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

        if gapped and serviced_devices == 0 and observations_extracted == 0 and not budget_starved:
            # Same systemic-degradation contract as the head pass (a
            # budget-starved run is a clean back-off, not degradation). The raise
            # also breaks the self-retrigger cascade below — a wholly-failing
            # backfill must not re-trigger itself forever.
            raise LotekException(
                message=(
                    f"No devices could be backfilled for integration {integration_id}: "
                    f"{len(failed_devices)} failed, {len(deferred_devices)} deferred of "
                    f"{len(gapped)}. See the per-device errors in this action's activity log."
                )
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
        )
        result = {
            'observations_extracted': observations_extracted,
            'devices_failed': failed_devices,
            'devices_deferred': deferred_devices,
            'gaps_closed': gaps_closed,
        }
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
                config=BackfillObservationsConfig(triggered_by="backfill_observations")
            )
        except Exception as e:
            # The next head pass will re-trigger; losing one cascade step is fine.
            logger.warning(
                f"Could not re-trigger backfill for integration {integration_id}: {describe_exception(e)}"
            )
    return result
