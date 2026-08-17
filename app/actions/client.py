import asyncio
import httpx
import logging
import pydantic
import time

from datetime import datetime, timedelta, timezone
from pydantic import BaseModel
from typing import Optional
from app.services.state import IntegrationStateManager


DEFAULT_TIMEOUT = (3.1, 20)
DEFAULT_LOOKBACK_DAYS = 60
# Statuses from the login endpoint that mean the credentials themselves were refused.
# Lotek answers a bad login with 400; 401/403 are included in case that ever changes.
CREDENTIAL_REJECTION_STATUSES = frozenset({400, 401, 403})


logger = logging.getLogger(__name__)
state_manager = IntegrationStateManager()

# One shared client for all Lotek requests, instead of a fresh AsyncClient per
# call. get_positions runs once per device per window (hundreds of devices on
# the big integrations), and building a client per call meant a fresh
# DNS + TCP + TLS handshake every time — the dominant source of outbound
# connection-acquisition failures in prod (GUNDI-5620). Same lazy
# getter/closer pattern as webhooks._get_diagnostic_client; closed from the
# FastAPI lifespan.
# Cookie note: the client is shared across integrations (different Lotek
# accounts). Lotek auth is header-only (Bearer); the API sets only Azure
# load-balancer affinity cookies (ARRAffinity*), which carry no account or
# session identity (verified 2026-08-17). Re-verify if Lotek ever moves to
# cookie sessions.
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=15.0, pool=5.0),
            # Invariant: max_connections >= cloud_run_concurrency (4) *
            # handlers.FETCH_CONCURRENCY (5) = 20 peak simultaneous requests.
            # 32 leaves headroom so bumping either knob doesn't silently start
            # queuing on the pool (PoolTimeout is breaker-feeding).
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=10),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class LotekException(Exception):
    def __init__(self, message: str, error: Optional[Exception] = None, status_code: int = 500):
        self.status_code = status_code
        self.message = message
        self.error = error
        super().__init__(self.__str__())

    def __str__(self) -> str:
        base = f"{self.status_code}: {self.message}"
        if self.error is not None:
            return f"{base} | Error: {self.error}"
        return base


class LotekUnauthorizedException(LotekException):
    """The login endpoint refused the credentials. Integration-wide and fatal:
    callers abort the whole run rather than repeat the failure per device."""
    def __init__(self, message: str = "Unauthorized", error: Optional[Exception] = None, status_code: int = 401):
        super().__init__(message=message, error=error, status_code=status_code)


class LotekTokenExpiredException(LotekException):
    """A 401 on a data call with a (possibly stale) cached token. The cached token
    is cleared before raising, so a retry re-authenticates; if it persists it is a
    per-device problem, NOT proof of refused credentials. Deliberately not a
    subclass of LotekUnauthorizedException so it never triggers the fatal path."""
    def __init__(self, message: str = "Token expired", error: Optional[Exception] = None, status_code: int = 401):
        super().__init__(message=message, error=error, status_code=status_code)


class LotekPosition(BaseModel):
    ChannelStatus: str
    UploadTimeStamp: datetime
    Latitude: float
    Longitude: float
    Altitude: float
    ECEFx: int
    ECEFy: int
    ECEFz: int
    RxStatus: int
    PDOP: float
    MainV: float
    BkUpV: float
    Temperature: float
    FixDuration: int
    bHasTempVoltage: bool
    DevName: str
    DeltaTime: int
    FixType: int
    CEPRadius: int
    CRC: int
    DeviceID: int
    RecDateTime: datetime


class LotekDevice(BaseModel):
    nDeviceID: str
    strSpecialID: str
    dtCreated: datetime
    strSatellite: str


def _to_utc(val: datetime) -> datetime:
    '''Normalize a datetime to UTC; naive datetimes are assumed to be UTC.'''
    if not val.tzinfo:
        return val.replace(tzinfo=timezone.utc)
    return val.astimezone(timezone.utc)


# Serializes the read-login-store sequence below, PER INTEGRATION. Device
# fetches run with bounded concurrency (handlers.FETCH_CONCURRENCY), so after
# a 401 clears the cached token, several coroutines can find it missing at
# once — without the lock each would log in separately, and Lotek logins
# invalidate the account's previous token, so concurrent logins invalidate
# each other in a loop. Per-integration (not global) because a login is slow
# network I/O (connect 10s + read 30s worst case): one integration's re-auth
# must not stall every other integration's fetches on the worker.
_token_locks: dict = {}
# A refused login is cached briefly and re-raised to concurrent waiters:
# without this, every task in an in-flight chunk performs its own real login
# attempt against an account that just rejected the password — the exact
# lockout risk the no-retry policy on LotekUnauthorizedException exists to
# avoid. The cooldown clears well before the next scheduled run, so fixed
# credentials are picked up on the following tick.
_login_rejections: dict = {}
LOGIN_REJECTION_COOLDOWN_SECONDS = 60.0


def _get_token_lock(integration_id: str) -> asyncio.Lock:
    lock = _token_locks.get(integration_id)
    if lock is None:
        lock = _token_locks[integration_id] = asyncio.Lock()
    return lock


async def get_token(integration, auth):
    integration_id = str(integration.id)
    async with _get_token_lock(integration_id):
        rejection = _login_rejections.get(integration_id)
        if rejection is not None:
            rejected_at, rejection_exc = rejection
            if time.monotonic() - rejected_at < LOGIN_REJECTION_COOLDOWN_SECONDS:
                raise rejection_exc
            del _login_rejections[integration_id]
        saved_token = await state_manager.get_state(
            integration_id,
            "pull_observations",
            "token"
        )
        if not saved_token:
            try:
                token = await get_token_from_api(integration, auth)
            except LotekUnauthorizedException as e:
                _login_rejections[integration_id] = (time.monotonic(), e)
                raise
            await state_manager.set_state(
                integration_id,
                "pull_observations",
                {"token": token},
                "token"
            )
        else:
            token = saved_token.get("token")

    return token


async def invalidate_token(integration, token):
    """Compare-and-delete the cached token after a 401 on a data call.

    Unconditional deletion is wrong under concurrent fetches: a slow request
    still carrying the OLD token can 401 after a peer has already re-logged-in
    and cached a FRESH one — deleting then would discard the valid token and
    force another login, which (per Lotek semantics) invalidates the fresh
    token for every peer mid-request with it. Only delete if the cache still
    holds the exact token that got the 401.
    """
    integration_id = str(integration.id)
    async with _get_token_lock(integration_id):
        saved_token = await state_manager.get_state(
            integration_id, "pull_observations", "token"
        )
        if saved_token and saved_token.get("token") == token:
            await state_manager.delete_state(
                integration_id, "pull_observations", "token"
            )

async def get_token_from_api(integration, auth):
    params = {
        "grant_type": "password",
        "username": auth.username,
        "password": auth.password.get_secret_value()
    }
    session = _get_client()
    try:
        base_url = integration.base_url or 'https://webservice.lotek.com/API'
        response = await session.post(base_url + "/user/login", data=params)
        response.raise_for_status()
    except httpx.HTTPStatusError as ex:
        msg = f'Lotek login failed for user {auth.username}. Caught exception: {ex}'
        status_code = ex.response.status_code
        if status_code in CREDENTIAL_REJECTION_STATUSES:
            # A rejected login is an integration-wide credentials problem, not a
            # per-device blip, and callers abort the whole run on it. Lotek answers
            # a bad login with 400. Server errors and rate limits are NOT credential
            # problems — reporting them as such would tell operators their password
            # is wrong when Lotek is merely down.
            raise LotekUnauthorizedException(message=msg, error=ex, status_code=status_code)
        raise LotekException(message=msg, error=ex, status_code=status_code)
    else:
        data = response.json()
        return data.get('access_token', None)

async def get_devices(integration, auth):
    try:
        token = await get_token(integration, auth)
        headers = {
            'Authorization': f"Bearer {token}",
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        }
        session = _get_client()
        base_url = integration.base_url or 'https://webservice.lotek.com/API'
        response = await session.get(base_url + "/devices", headers=headers)
        response.raise_for_status()
    except httpx.HTTPStatusError as ex:
        if ex.response.status_code == 401:
            msg = "Received status code 401 - Token expired, fetching a new one..."
            logger.info(msg)
            await invalidate_token(integration, token)
            raise LotekTokenExpiredException(message=f"401 Response from Lotek API", error=ex)
        else:
            msg = f'Lotek get_devices failed for user {auth.username}. Caught exception: {ex}'
            raise LotekException(status_code=ex.response.status_code, message=msg, error=ex)
    else:
        data = response.json()
        devices = [LotekDevice(**device) for device in data]
        return devices

async def get_positions(device_id, auth, integration, start_datetime=None, end_datetime=None, geo_only=False):
    token = await get_token(integration, auth)
    headers = {
        'Authorization': f"Bearer {token}",
        'Accept': 'application/json',
        'Content-Type': 'application/json'
    }
    if not start_datetime:
        start_datetime = datetime.now(tz=timezone.utc) - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    if not end_datetime:
        end_datetime = datetime.now(tz=timezone.utc)

    params = {
        'deviceId': device_id,
        'from': _to_utc(start_datetime).strftime('%Y-%m-%dT%H:%M:%S+0000'),
        'to': _to_utc(end_datetime).strftime('%Y-%m-%dT%H:%M:%S+0000')
    }

    session = _get_client()
    try:
        logger.debug('Getting positions for user: %s, params: %s', auth.username, params)
        base_url = integration.base_url or 'https://webservice.lotek.com/API'
        response = await session.get(base_url + "/positions/findByUploadDate", params=params, headers=headers)
        response.raise_for_status()
    except httpx.HTTPStatusError as ex:
        if ex.response.status_code == 400:
            logger.info("Received status code 400 - Lotek throws this when there are no data")
            return []
        if ex.response.status_code == 401:
            msg = "Received status code 401 - Token expired, fetching a new one..."
            logger.info(msg)
            await invalidate_token(integration, token)
            raise LotekTokenExpiredException(message=f"401 Response from Lotek API", error=ex)

        msg = f'Lotek get_positions failed for user {auth.username}. Caught exception: {ex}'
        logger.exception(
            msg,
            extra={
                "attention_needed": True,
                "device_id": str(device_id),
                "integration_type": "lotek"
            }
        )
        raise LotekException(status_code=ex.response.status_code, error=ex, message=msg)
    else:
        positions = response.json()
        logger.debug('Got %d positions using params=%s', len(positions), params)
        results = [LotekPosition(**position) for position in positions if not (geo_only and (position['Latitude'] == 0 or position['Longitude'] == 0))]
        return results
