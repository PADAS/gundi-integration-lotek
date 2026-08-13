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
from app.actions.configurations import AuthenticateConfig, BackfillObservationsConfig, PullObservationsConfig
from app.actions.core import action_title
from app.actions.device_state import DeviceState
from app.services.action_scheduler import trigger_action
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


def _retry_attempts(run_started_at):
    # Past the soft deadline, don't spend the remaining budget on retries.
    return 1 if _deadline_exceeded(run_started_at) else RETRY_ATTEMPTS


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
    # Log only the id: the full Integration object embeds auth config data in
    # plaintext (same leak family as the action-runner _handle_error ticket).
    logger.info(f"Executing pull_observations action for integration {integration.id}...")

    # The clock starts before get_devices: it spends the same action budget.
    run_started = datetime.now(tz=timezone.utc)
    auth = get_auth_config(integration)
    try:
        async for attempt in stamina.retry_context(on=RETRYABLE_ERRORS, attempts=RETRY_ATTEMPTS, wait_initial=RETRY_WAIT_INITIAL, wait_jitter=RETRY_WAIT_JITTER, wait_max=RETRY_WAIT_MAX):
            with attempt:
                device_list = await client.get_devices(integration, auth)
    except Exception as e:
        message = f"Error fetching devices from Lotek. Integration ID: {integration.id} Exception: {describe_exception(e)}"
        logger.exception(message)
        await log_action_activity(
            integration_id=str(integration.id),
            action_id="pull_observations",
            title=message,
            level=LogLevel.ERROR
        )
        raise  # bare: preserve the original traceback

    logger.info(f"Extracted {len(device_list)} devices from Lotek for inbound: {integration.id}")
    present_time = datetime.now(tz=timezone.utc)
    guards = RunGuards(run_started)
    observations_extracted = 0
    failed_devices = []
    deferred_devices = []
    serviced_devices = 0
    # Only reflects devices actually processed this run — a device deferred by
    # the deadline/breaker before its gap status is checked doesn't trigger
    # backfill this cycle. Self-correcting: the next run's head pass reaches it
    # and triggers then, same as the worst-case import delay the design accepts.
    any_open_gap = False
    for i, device in enumerate(device_list):
        if reason := guards.should_stop():
            deferred_devices = [d.nDeviceID for d in device_list[i:]]
            await _log_deferral(integration, "pull_observations", reason, deferred_devices)
            break
        try:
            sent, device_failed, transport_failure, state = await _head_pass_device(
                device, integration, auth, action_config, present_time, guards
            )
        except LotekUnauthorizedException:
            raise
        except Exception as e:
            # Sending to Gundi and checkpointing can fail too, and a device that fetched
            # fine but failed downstream must not take the rest of the batch with it.
            message = (
                f"Failed to process device {device.nDeviceID} for integration "
                f"{integration.id}: {describe_exception(e)}"
            )
            logger.exception(message)
            await log_action_activity(
                integration_id=str(integration.id),
                action_id="pull_observations",
                title=message,
                level=LogLevel.ERROR
            )
            failed_devices.append(device.nDeviceID)
            guards.record(transport_failure=False)
            continue
        # Single recording site: transport failures arm the breaker, anything
        # else (success included) breaks the consecutive streak.
        guards.record(transport_failure=transport_failure)
        observations_extracted += sent
        if state.has_gap:
            any_open_gap = True
        if device_failed:
            failed_devices.append(device.nDeviceID)
        else:
            serviced_devices += 1

    if failed_devices:
        message = (
            f"Pulled observations with {len(failed_devices)} of {len(device_list)} device(s) "
            f"failing for integration {integration.id}: {_summarize_ids(failed_devices)}. "
            f"They will be retried on the next run."
        )
        logger.warning(message)
        await log_action_activity(
            integration_id=str(integration.id),
            action_id="pull_observations",
            title=message,
            level=LogLevel.WARNING
        )

    if device_list and serviced_devices == 0 and observations_extracted == 0:
        # Zero progress: nothing serviced and nothing delivered — whether every
        # device failed or the rails deferred them all, this is systemic
        # degradation and must alert rather than publish action_complete. A
        # device that delivered part of its window before failing still counts
        # as progress, so partial runs stay warnings. This comes after the
        # summary so the worst case keeps its device-naming log.
        raise LotekException(
            message=(
                f"No devices could be serviced for integration {integration.id}: "
                f"{len(failed_devices)} failed, {len(deferred_devices)} deferred of "
                f"{len(device_list)}. See the per-device errors in this action's activity log."
            )
        )

    if any_open_gap:
        try:
            lease = await state_manager.get_state(
                str(integration.id), "backfill_observations", BACKFILL_LEASE_SOURCE
            )
            if not lease:
                # backfill_observations is an InternalActionConfiguration with no
                # persisted portal config, so this MUST carry a non-empty config
                # override — a bare trigger_action(..., "backfill_observations")
                # publishes an empty config_overrides, which execute_action reads
                # as "no config at all" and 404s before the handler ever runs.
                await trigger_action(
                    str(integration.id), "backfill_observations",
                    config=BackfillObservationsConfig(triggered_by="pull_observations")
                )
        except Exception as e:
            # The head pass succeeded; a failed trigger must not fail the run.
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
        try:
            await log_action_activity(
                integration_id=integration_id,
                action_id=action_id,
                title=message,
                level=LogLevel.WARNING
            )
        except Exception:
            logger.warning(f"Could not publish state-reset warning for device {device_id}")
        return None


async def _load_device_state(integration_id, device_id, present_time, action_config):
    """Returns (state, is_new). is_new means no usable saved state existed and
    the returned state is the first-run initialization (gap birth)."""
    state = await _read_device_state(integration_id, device_id)
    if state is not None:
        return state, False
    # First run: the head pass starts at the freshness floor; everything older,
    # back to the configured lookback, becomes the device's one and only gap —
    # the deliberate historical import. It only ever shrinks from here.
    head_start = present_time - timedelta(hours=action_config.max_data_age_hours)
    gap_start = present_time - timedelta(days=action_config.default_lookback_days)
    if gap_start < head_start:
        return DeviceState(high_water=head_start, gap_start=gap_start, gap_end=head_start), True
    return DeviceState(high_water=head_start), True


async def _save_device_state_fields(integration_id, device_id, updates):
    """Merge-save: re-read the current blob and overwrite only the fields this
    writer owns (head pass: high_water; backfill: gap_*/last_backfilled).

    The two actions can interleave — the Redis lease only serializes backfill
    against backfill — and whole-blob writes from a stale snapshot were
    resurrecting closed gaps and rewinding the head cursor (review finding:
    lost-update race). The re-read shrinks the race window from the whole
    action duration to milliseconds; re-sends cover whatever remains.
    """
    current = await state_manager.get_state(integration_id, "pull_observations", device_id)
    merged = {**(current or {}), **updates}
    await state_manager.set_state(integration_id, "pull_observations", merged, device_id)


async def _fetch_window(device, integration, auth, config, lower_date, upper_date, guards, action_id):
    """Fetch + transform one window for one device.

    Returns (cdip_positions | None, transport_failure). None means the fetch
    failed — already logged and classified; the caller marks the device failed
    and records transport_failure for the circuit breaker. Raises only
    LotekUnauthorizedException (integration-wide).
    """
    integration_id = str(integration.id)
    try:
        async for attempt in stamina.retry_context(on=RETRYABLE_ERRORS, attempts=_retry_attempts(guards.run_started_at), wait_initial=RETRY_WAIT_INITIAL, wait_jitter=RETRY_WAIT_JITTER, wait_max=RETRY_WAIT_MAX):
            with attempt:
                positions = await client.get_positions(device.nDeviceID, auth, integration, lower_date, upper_date, True)
        logger.info(f"Extracted {len(positions)} obs from Lotek for device: {device.nDeviceID} between {lower_date} and {upper_date}.")
        # Transform inside the try: a malformed payload is a per-device, fetch-class
        # failure and must get the same device/date-window log and isolation.
        return filter_and_transform_positions(positions, integration, config), False
    except LotekUnauthorizedException:
        # Credentials are an integration-wide problem: every remaining device
        # would fail the same way, so fail fast instead of N identical errors.
        raise
    except httpx.TransportError as e:
        # WARNING + breaker-feeding: enough timeouts/transport failures in a
        # row mean Lotek-wide degradation, not a bad device.
        message = f"Error fetching positions from Lotek. Device: {device.nDeviceID}. Dates: [{lower_date},{upper_date}]. Integration ID: {integration_id} Exception: {describe_exception(e)}"
        logger.warning(message, exc_info=True)
        await log_action_activity(
            integration_id=integration_id,
            action_id=action_id,
            title=message,
            level=LogLevel.WARNING
        )
        return None, True
    except Exception as e:
        # Deliberately broad: malformed data from Lotek surfaces as KeyError or
        # pydantic.ValidationError out of the client's parsing, and those must
        # not take down the devices behind this one either. CancelledError is a
        # BaseException, so the action timeout still unwinds normally.
        # ERROR, not WARNING: unlike a transient timeout (httpx.TransportError,
        # above), a data-shape break is permanent and won't self-heal on retry —
        # it must stay visible to health/alerting or it can persist unnoticed
        # forever (review finding).
        message = f"Error fetching positions from Lotek. Device: {device.nDeviceID}. Dates: [{lower_date},{upper_date}]. Integration ID: {integration_id} Exception: {describe_exception(e)}"
        logger.exception(message)
        await log_action_activity(
            integration_id=integration_id,
            action_id=action_id,
            title=message,
            level=LogLevel.ERROR
        )
        return None, False


async def _deliver(cdip_positions, device, integration, action_id):
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
            logger.info(f"{len(cdip_positions)} observations pulled successfully for device {device.nDeviceID} integration ID: {integration.id}.")
            for i, batch in enumerate(generate_batches(cdip_positions)):
                logger.info(f'Sending observations batch #{i}: {len(batch)} observations. Device: {device.nDeviceID}')
                await gundi_tools.send_observations_to_gundi(observations=batch, integration_id=integration.id)
                observations_sent += len(batch)
    except Exception as e:
        message = (
            f"Error delivering observations for device {device.nDeviceID}. Integration ID: "
            f"{integration.id} Exception: {describe_exception(e)}"
        )
        # needs_attention drives log-based alerting on delivery failures (template
        # convention) — kept from the pre-isolation send-loop handler.
        logger.exception(message, extra={
            'needs_attention': True,
            'integration_id': integration_id,
            'action_id': action_id
        })
        await log_action_activity(
            integration_id=integration_id,
            action_id=action_id,
            title=message,
            level=LogLevel.ERROR
        )
        return observations_sent, True
    return observations_sent, False


async def _head_pass_device(device, integration, auth, action_config, present_time, guards):
    """Fetch, deliver and checkpoint one device's freshest window.

    Returns (observations_sent, device_failed, transport_failure, state).
    Raises only for integration-wide problems; per-device problems are
    reported through the returned flags so the caller can keep going (and
    record transport_failure for the circuit breaker in one place).
    Fetch-phase transport failures are WARNINGs (transient while Lotek is
    slow; devices_failed tracks them); data-shape, delivery and checkpoint
    failures stay ERRORs.
    """
    integration_id = str(integration.id)
    freshness_floor = present_time - timedelta(hours=action_config.max_data_age_hours)
    state, is_new = await _load_device_state(integration_id, device.nDeviceID, present_time, action_config)

    if state.high_water < freshness_floor:
        # Bounded staleness (GUNDI-5602, deliberate): anything the cursor still
        # owed beyond max_data_age_hours is dropped permanently — never added
        # to the gap — so catch-up cost cannot compound.
        message = (
            f"Dropping stale range [{state.high_water.isoformat()}, {freshness_floor.isoformat()}] "
            f"for device {device.nDeviceID}: older than max_data_age_hours="
            f"{action_config.max_data_age_hours}. Integration ID: {integration_id}"
        )
        logger.warning(message)
        try:
            await log_action_activity(
                integration_id=integration_id,
                action_id="pull_observations",
                title=message,
                level=LogLevel.WARNING
            )
        except Exception as e:
            # Informational: a pubsub blip must not keep a stale device from
            # ever advancing its cursor (review finding).
            logger.warning(f"Could not publish stale-drop warning for device {device.nDeviceID}: {describe_exception(e)}")

    lower_date = max(state.high_water - HEAD_LATE_UPLOAD_OVERLAP, freshness_floor)
    cdip_positions, transport_failure = await _fetch_window(
        device, integration, auth, action_config, lower_date, present_time, guards, "pull_observations"
    )
    if cdip_positions is None:
        return 0, True, transport_failure, state

    if not cdip_positions:
        # Purely informational; must never affect the device's outcome. A pubsub blip
        # here must not mark a healthy quiet device as failed or stall its cursor.
        message = f"No positions fetched for device {device.nDeviceID} integration ID: {integration.id}."
        logger.info(message)
        try:
            await log_action_activity(
                integration_id=str(integration.id),
                action_id="pull_observations",
                title=message,
                level=LogLevel.WARNING
            )
        except Exception as e:
            logger.warning(f"Could not publish activity log for device {device.nDeviceID}: {describe_exception(e)}")

    observations_sent, delivery_failed = await _deliver(cdip_positions, device, integration, "pull_observations")
    if delivery_failed:
        # The breaker watches Lotek, not Gundi: a delivery failure is not
        # evidence of Lotek-wide degradation.
        return observations_sent, True, False, state

    # Advance the cursor to the queried upper bound (upload time), not recorded_at.
    # Queries are by upload date, so wall clock is the correct cursor, and it must
    # advance even when a device returns no positions. On failure the cursor stays
    # untouched so the head window is re-fetched next run (re-sends are tolerated,
    # silent skips are not).
    state.high_water = present_time
    # The head pass owns only high_water; a first run also births the full
    # document (the gap). Everything else belongs to the backfill (merge-save,
    # see _save_device_state_fields).
    updates = state.dict() if is_new else {"high_water": state.high_water}
    try:
        await _save_device_state_fields(integration_id, device.nDeviceID, updates)
    except Exception as e:
        message = (
            f"Error saving cursor for device {device.nDeviceID}. Integration ID: "
            f"{integration.id} Exception: {describe_exception(e)}"
        )
        logger.exception(message)
        await log_action_activity(
            integration_id=integration_id,
            action_id="pull_observations",
            title=message,
            level=LogLevel.ERROR
        )
        return observations_sent, True, False, state

    return observations_sent, False, False, state


async def _backfill_device(device, state, integration, auth, pull_config, guards):
    """Close up to BACKFILL_MAX_WINDOWS_PER_DEVICE oldest windows of one
    device's gap.

    Returns (observations_sent, device_failed, transport_failure, gap_closed).
    gap_start advances only past windows that were actually delivered, so a
    failure re-fetches the same window next trigger (re-sends are tolerated,
    silent skips are not).
    """
    integration_id = str(integration.id)
    observations_sent = 0
    device_failed = False
    transport_failure = False
    for _ in range(BACKFILL_MAX_WINDOWS_PER_DEVICE):
        if not state.has_gap:
            break
        window_start = state.gap_start
        upper_date = min(state.gap_end, state.gap_start + BACKFILL_WINDOW)

        cdip_positions, transport_failure = await _fetch_window(
            device, integration, auth, pull_config, window_start, upper_date, guards, "backfill_observations"
        )
        if cdip_positions is None:
            device_failed = True
            break

        sent, delivery_failed = await _deliver(cdip_positions, device, integration, "backfill_observations")
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

    # Advances even on zero progress (device_failed on the first window):
    # deliberate LRS-fairness trade-off so one permanently-broken device
    # doesn't monopolize the front of the backfill queue. The zero-progress
    # raise is the actual safety net when it's the only gapped device.
    state.last_backfilled = datetime.now(tz=timezone.utc)
    await _save_device_state_fields(
        integration_id, device.nDeviceID, {"last_backfilled": state.last_backfilled}
    )
    return observations_sent, device_failed, transport_failure, not state.has_gap


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
    auth = get_auth_config(integration)
    pull_config = get_pull_config(integration)  # max_pdop applies to backfilled data too

    if not pull_config.run_on_schedule:
        # The operator's pause toggle must also stop the cascade: internal
        # actions bypass the runner's skippable_pull pause check, and backfill
        # is only ever machine-triggered — a paused integration skips cleanly
        # (review finding).
        logger.info(f"Integration {integration_id} is paused (run_on_schedule=false); skipping backfill.")
        return {"skipped": True, "reason": "integration_paused"}

    got_lease = await state_manager.set_if_absent(
        integration_id,
        "backfill_observations",
        ttl_seconds=app_settings.MAX_ACTION_EXECUTION_TIME,
        source_id=BACKFILL_LEASE_SOURCE,
    )
    if not got_lease:
        logger.info(f"Backfill lease already held for integration {integration_id}; skipping run.")
        return {"skipped": True, "reason": "lease_held"}

    try:
        try:
            async for attempt in stamina.retry_context(on=RETRYABLE_ERRORS, attempts=RETRY_ATTEMPTS, wait_initial=RETRY_WAIT_INITIAL, wait_jitter=RETRY_WAIT_JITTER, wait_max=RETRY_WAIT_MAX):
                with attempt:
                    device_list = await client.get_devices(integration, auth)
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
        for i, (device, state) in enumerate(gapped):
            if reason := guards.should_stop():
                deferred_devices = [d.nDeviceID for d, _ in gapped[i:]]
                await _log_deferral(integration, "backfill_observations", reason, deferred_devices)
                break
            try:
                sent, device_failed, transport_failure, gap_closed = await _backfill_device(
                    device, state, integration, auth, pull_config, guards
                )
            except LotekUnauthorizedException:
                raise
            except Exception as e:
                message = (
                    f"Failed to backfill device {device.nDeviceID} for integration "
                    f"{integration_id}: {describe_exception(e)}"
                )
                logger.exception(message)
                await log_action_activity(
                    integration_id=integration_id,
                    action_id="backfill_observations",
                    title=message,
                    level=LogLevel.ERROR
                )
                failed_devices.append(device.nDeviceID)
                guards.record(transport_failure=False)
                continue
            # Single recording site, mirroring the head-pass loop.
            guards.record(transport_failure=transport_failure)
            observations_extracted += sent
            gaps_closed += int(gap_closed)
            if device_failed:
                failed_devices.append(device.nDeviceID)
            else:
                serviced_devices += 1

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

        if gapped and serviced_devices == 0 and observations_extracted == 0:
            # Same systemic-degradation contract as the head pass. The raise
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
        breaker_hot = guards.consecutive_transport_failures >= BREAKER_THRESHOLD
        gaps_remaining = any(state.has_gap for _, state in gapped) and not breaker_hot
        result = {
            'observations_extracted': observations_extracted,
            'devices_failed': failed_devices,
            'devices_deferred': deferred_devices,
            'gaps_closed': gaps_closed,
        }
    finally:
        await state_manager.delete_state(integration_id, "backfill_observations", BACKFILL_LEASE_SOURCE)

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
