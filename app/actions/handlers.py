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
        return None

    def record(self, transport_failure: bool):
        if transport_failure:
            self.consecutive_transport_failures += 1
        else:
            self.consecutive_transport_failures = 0


async def _log_deferral(integration, action_id, reason, deferred_ids):
    listed = ', '.join(deferred_ids[:MAX_DEVICES_IN_SUMMARY])
    if len(deferred_ids) > MAX_DEVICES_IN_SUMMARY:
        listed += f" and {len(deferred_ids) - MAX_DEVICES_IN_SUMMARY} more"
    message = (
        f"Stopping early ({reason}) for integration {integration.id}: deferring "
        f"{len(deferred_ids)} device(s) to the next run: {listed}."
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
    logger.info(f"Executing pull_observations action with integration {integration} and action_config {action_config}...")

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
        raise e

    logger.info(f"Extracted {len(device_list)} devices from Lotek for inbound: {integration.id}")
    present_time = datetime.now(tz=timezone.utc)
    guards = RunGuards(run_started)
    observations_extracted = 0
    failed_devices = []
    deferred_devices = []
    serviced_devices = 0
    any_open_gap = False
    for i, device in enumerate(device_list):
        if reason := guards.should_stop():
            deferred_devices = [d.nDeviceID for d in device_list[i:]]
            await _log_deferral(integration, "pull_observations", reason, deferred_devices)
            break
        try:
            sent, device_failed, state = await _head_pass_device(
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
        observations_extracted += sent
        if state is not None and state.has_gap:
            any_open_gap = True
        if device_failed:
            failed_devices.append(device.nDeviceID)
        else:
            serviced_devices += 1

    if failed_devices:
        listed = ', '.join(failed_devices[:MAX_DEVICES_IN_SUMMARY])
        if len(failed_devices) > MAX_DEVICES_IN_SUMMARY:
            listed += f" and {len(failed_devices) - MAX_DEVICES_IN_SUMMARY} more"
        message = (
            f"Pulled observations with {len(failed_devices)} of {len(device_list)} device(s) "
            f"failing for integration {integration.id}: {listed}. They will be retried on the "
            f"next run."
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
                await trigger_action(str(integration.id), "backfill_observations")
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


async def _load_device_state(integration_id, device_id, present_time, action_config):
    saved = await state_manager.get_state(integration_id, "pull_observations", device_id)
    if saved:
        try:
            return DeviceState.parse_obj(saved)
        except pydantic.ValidationError as e:
            logger.debug(f"Failed to parse saved state for device {device_id}, starting fresh. Error: {e}")
    # First run: the head pass starts at the freshness floor; everything older,
    # back to the configured lookback, becomes the device's one and only gap —
    # the deliberate historical import. It only ever shrinks from here.
    head_start = present_time - timedelta(hours=action_config.max_data_age_hours)
    gap_start = present_time - timedelta(days=action_config.default_lookback_days)
    if gap_start < head_start:
        return DeviceState(high_water=head_start, gap_start=gap_start, gap_end=head_start)
    return DeviceState(high_water=head_start)


async def _head_pass_device(device, integration, auth, action_config, present_time, guards):
    """Fetch, deliver and checkpoint one device's freshest window.

    Returns (observations_sent, device_failed, state). Raises only for
    integration-wide problems; per-device problems are reported through the
    returned flag so the caller can keep going. Fetch-phase failures are
    WARNINGs (transient while Lotek is slow; devices_failed tracks them);
    delivery/checkpoint failures stay ERRORs.
    """
    integration_id = str(integration.id)
    freshness_floor = present_time - timedelta(hours=action_config.max_data_age_hours)
    state = await _load_device_state(integration_id, device.nDeviceID, present_time, action_config)

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
        await log_action_activity(
            integration_id=integration_id,
            action_id="pull_observations",
            title=message,
            level=LogLevel.WARNING
        )

    lower_date = max(state.high_water, freshness_floor)
    try:
        async for attempt in stamina.retry_context(on=RETRYABLE_ERRORS, attempts=_retry_attempts(guards.run_started_at), wait_initial=RETRY_WAIT_INITIAL, wait_jitter=RETRY_WAIT_JITTER, wait_max=RETRY_WAIT_MAX):
            with attempt:
                positions = await client.get_positions(device.nDeviceID, auth, integration, lower_date, present_time, True)
        logger.info(f"Extracted {len(positions)} obs from Lotek for device: {device.nDeviceID} between {lower_date} and {present_time}.")
        # Transform inside the try: a malformed payload is a per-device, fetch-class
        # failure and must get the same device/date-window log and isolation.
        cdip_positions = filter_and_transform_positions(positions, integration, action_config)
    except LotekUnauthorizedException:
        # Credentials are an integration-wide problem: every remaining device
        # would fail the same way, so fail fast instead of N identical errors.
        raise
    except Exception as e:
        # Deliberately broad: malformed data from Lotek surfaces as KeyError or
        # pydantic.ValidationError out of the client's parsing, and those must
        # not take down the devices behind this one either. CancelledError is a
        # BaseException, so the action timeout still unwinds normally.
        # WARNING, not ERROR: these recur daily while Lotek is slow, health
        # keys on ERROR count alone, and devices_failed already tracks them.
        message = f"Error fetching positions from Lotek. Device: {device.nDeviceID}. Dates: [{lower_date},{present_time}]. Integration ID: {integration_id} Exception: {describe_exception(e)}"
        logger.warning(message, exc_info=True)
        await log_action_activity(
            integration_id=integration_id,
            action_id="pull_observations",
            title=message,
            level=LogLevel.WARNING
        )
        return 0, True, state

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

    observations_sent = 0
    try:
        if cdip_positions:
            logger.info(f"{len(cdip_positions)} observations pulled successfully for device {device.nDeviceID} integration ID: {integration.id}.")
            for i, batch in enumerate(generate_batches(cdip_positions)):
                logger.info(f'Sending observations batch #{i}: {len(batch)} observations. Device: {device.nDeviceID}')
                await gundi_tools.send_observations_to_gundi(observations=batch, integration_id=integration.id)
                observations_sent += len(batch)
    except Exception as e:
        # Handled here rather than in the caller so batches already delivered keep
        # counting toward the run's total — otherwise a send/checkpoint failure after a
        # successful delivery reports "nothing delivered". Cursor stays untouched below,
        # so the un-delivered remainder is re-fetched next run (re-sends are tolerated,
        # silent skips are not).
        message = (
            f"Error delivering observations for device {device.nDeviceID}. Integration ID: "
            f"{integration.id} Exception: {describe_exception(e)}"
        )
        # needs_attention drives log-based alerting on delivery failures (template
        # convention) — kept from the pre-isolation send-loop handler.
        logger.exception(message, extra={
            'needs_attention': True,
            'integration_id': integration_id,
            'action_id': "pull_observations"
        })
        await log_action_activity(
            integration_id=integration_id,
            action_id="pull_observations",
            title=message,
            level=LogLevel.ERROR
        )
        return observations_sent, True, state

    # Advance the cursor to the queried upper bound (upload time), not recorded_at.
    # Queries are by upload date, so wall clock is the correct cursor, and it must
    # advance even when a device returns no positions. On failure the cursor stays
    # untouched so the head window is re-fetched next run (re-sends are tolerated,
    # silent skips are not).
    state.high_water = present_time
    try:
        await state_manager.set_state(
            integration_id,
            "pull_observations",
            state.dict(),
            device.nDeviceID
        )
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
        return observations_sent, True, state

    return observations_sent, False, state
