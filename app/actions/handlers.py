import httpx
import logging
import stamina
import pydantic

import app.services.gundi as gundi_tools
import app.actions.client as client
import app.settings.integration as settings

from datetime import datetime, timezone, timedelta

from app.services.errors import ConfigurationNotFound
from app.actions.client import LotekException, LotekUnauthorizedException
from app.services.utils import find_config_for_action
from app.actions.configurations import AuthenticateConfig, PullObservationsConfig
from app.services.activity_logger import activity_logger, log_action_activity
from app.services.state import IntegrationStateManager
from gundi_core.schemas.v2.gundi import LogLevel

logger = logging.getLogger(__name__)
state_manager = IntegrationStateManager()


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

async def action_auth(integration, action_config: AuthenticateConfig):
    logger.info(f"Executing auth action with integration {integration} and action_config {action_config}...")
    try:
        token = await client.get_token_from_api(integration, action_config)
    except LotekException as e:
        logger.exception(f"Auth unsuccessful for integration {integration.id}. Lotek returned 401 (Unauthorized). Exc: {e}")
        return {"valid_credentials": False, "message": "Invalid credentials"}
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

def filter_and_transform_positions(positions, integration):
    valid_positions = []
    for position in positions:
        try:
            if not position.Longitude or not position.Latitude:
                msg = f"Filtering {position} (bad location) for device {position.DeviceID}."
                logger.info(msg)
                continue

            cdip_pos = {
                "source": position.DeviceID,
                "source_name": position.DevName,
                'type': 'tracking-device',
                "recorded_at": ensure_timezone_aware(position.RecDateTime).isoformat(),
                "location": {
                    "lat": position.Latitude,
                    "lon": position.Longitude
                },
                "additional": position.dict(exclude={'DeviceID', 'Latitude', 'Longitude', 'RecDateTime'})
            }
            valid_positions.append(cdip_pos)
        except Exception as ex:
            logger.error(f"Failed to parse Lotek point: {position} for Integration ID {str(integration.id)}. Exception: {ex}")

    return valid_positions

def ensure_timezone_aware(val: datetime, default_tz: timezone = timezone.utc) -> datetime:
    if not val.tzinfo:
        val = val.replace(tzinfo=default_tz)
    return val

@activity_logger()
async def action_pull_observations(integration, action_config: PullObservationsConfig):
    logger.info(f"Executing pull_observations action with integration {integration} and action_config {action_config}...")

    auth = get_auth_config(integration)
    try:
        async for attempt in stamina.retry_context(on=LotekUnauthorizedException, attempts=3, wait_initial=1.0, wait_jitter=5.0, wait_max=32.0):
            with attempt:
                device_list = await client.get_devices(integration, auth)
    except Exception as e:
        message = f"Error fetching devices from Lotek. Integration ID: {integration.id} Exception: {e}"
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
    observations_extracted = 0
    for device in device_list:
        cdip_positions = []
        try:
            saved_state = await state_manager.get_state(str(integration.id), "pull_observations", device.nDeviceID)
            state = client.IntegrationState.parse_obj({"updated_at": saved_state.get("updated_at")})
        except pydantic.ValidationError as e:
            logger.debug(f"Failed to parse saved state for device {device.nDeviceID}, using default state. Error: {e}")
            state = client.IntegrationState()

        lower_date = max(present_time - timedelta(days=7), state.updated_at)
        while lower_date < present_time:
            upper_date = min(present_time, lower_date + timedelta(days=7))
            try:
                async for attempt in stamina.retry_context(on=LotekUnauthorizedException, attempts=3, wait_initial=1.0, wait_jitter=5.0, wait_max=32.0):
                    with attempt:
                        positions = await client.get_positions(device.nDeviceID, auth, integration, lower_date, upper_date, True)
                logger.info(f"Extracted {len(positions)} obs from Lotek for device: {device.nDeviceID} between {lower_date} and {upper_date}.")
            except httpx.HTTPError as e:
                message = f"Error fetching positions from Lotek. Device: {device.nDeviceID}. Dates: [{lower_date},{upper_date}]. Integration ID: {integration.id} Exception: {e}"
                logger.exception(message)
                await log_action_activity(
                    integration_id=str(integration.id),
                    action_id="pull_observations",
                    title=message,
                    level=LogLevel.ERROR
                )
                raise LotekException(message=message, error=e)
            cdip_positions.extend(filter_and_transform_positions(positions, integration))
            lower_date = upper_date

        if cdip_positions:
            logger.info(f"{len(cdip_positions)} observations pulled successfully for device {device.nDeviceID} integration ID: {integration.id}.")
            for i, batch in enumerate(generate_batches(cdip_positions)):
                try:
                    logger.info(f'Sending observations batch #{i}: {len(batch)} observations. Device: {device.nDeviceID}')
                    await gundi_tools.send_observations_to_gundi(observations=batch, integration_id=integration.id)
                except httpx.HTTPError as e:
                    msg = f'Sensors API returned error for integration_id: {str(integration.id)}. Exception: {e}'
                    logger.exception(msg, extra={
                        'needs_attention': True,
                        'integration_id': integration.id,
                        'action_id': "pull_observations"
                    })
                    raise e
                else:
                    observations_extracted += len(batch)

            latest_time = max(cdip_positions, key=lambda obs: obs["recorded_at"])["recorded_at"]
            state = {"updated_at": latest_time}

            await state_manager.set_state(
                str(integration.id),
                "pull_observations",
                state,
                device.nDeviceID
            )
        else:
            message = f"No positions fetched for device {device.nDeviceID} integration ID: {integration.id}."
            logger.info(message)
            await log_action_activity(
                integration_id=str(integration.id),
                action_id="pull_observations",
                title=message,
                level=LogLevel.WARNING
            )

    return {'observations_extracted': observations_extracted}
