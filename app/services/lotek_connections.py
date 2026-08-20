import hashlib
import logging
import time
import uuid
from contextlib import asynccontextmanager

import redis.asyncio as redis
import stamina

from app import settings

logger = logging.getLogger(__name__)


class NoConnectionSlot(Exception):
    """Raised when the Lotek connection budget for a username is exhausted."""


# Atomic acquire: purge expired slots, re-grant to an existing holder, then add
# a new slot only if under the ceiling. KEYS[1]=zset key.
# ARGV: now, expiry, ceiling, token, key_ttl.
# Returns 1 if acquired (or already held), 0 if at capacity.
#
# The ZSCORE fast-path makes the script idempotent for a given token. Without
# it, a lost reply on the acquire that filled the last slot made the stamina
# retry refuse the very caller that owns the slot, stranding it for the full
# TTL (review finding). Re-granting also refreshes the expiry so a retried
# holder does not inherit an about-to-expire slot.
#
# The key itself gets a TTL on every path: members carry logical expiry in
# their score but are only purged by a LATER acquire, so an account that stops
# being used would leave its zset behind forever. The TTL comfortably outlives
# the longest-lived member, so it can never expire a key with live holders.
_ACQUIRE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[5]))
if redis.call('ZSCORE', KEYS[1], ARGV[4]) then
    redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
    return 1
end
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[3]) then
    redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
    return 1
end
return 0
"""


def connection_key(username: str) -> str:
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:16]
    return f"lotek:connections:{digest}"


_shared_client = None


def _client() -> redis.Redis:
    global _shared_client
    if _shared_client is None:
        _shared_client = redis.Redis(
            host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_STATE_DB
        )
    return _shared_client


async def close_connection_client() -> None:
    """Close the shared Redis client on shutdown, like every other module-level
    client the FastAPI lifespan closes. Without this the connections are only
    reclaimed by __del__ after the event loop is gone, which logs a
    "RuntimeError: Event loop is closed" traceback per pooled connection on
    every restart (observed on the local stage stack).
    """
    global _shared_client
    if _shared_client is not None:
        await _shared_client.aclose()
        _shared_client = None


@asynccontextmanager
async def lotek_slot(username: str, *, ttl_seconds: int = 300):
    """Acquire one Lotek connection slot for `username`, shared across every
    shard/backfill invocation on the same Redis. Raises NoConnectionSlot if at
    capacity. (Movebank-connector pattern: once the head pass fans out into
    concurrently-running shard sub-actions, an out-of-process cap is the only
    thing bounding simultaneous requests against one Lotek account.)

    Slots are members of a per-username sorted set scored by expiry time, so a
    crashed holder's slot self-expires (purged on the next acquire) rather than
    leaking the budget permanently. TTL sizing: the slot is released between
    stamina attempts (re-acquired per attempt), so retries never extend a
    hold. What CAN extend one is a 401-triggered re-login inside the request
    (token invalidated mid-flight — callers pre-warm the token before
    acquiring, so this is the rare path, but it stacks a login's
    connect+read on top of the request's own, plus queueing on the
    per-integration token lock). 300s covers that worst case; a hold that
    somehow outlives the TTL only soft-fails (one extra admission until the
    next purge).
    """
    client = _client()
    key = connection_key(username)
    token = str(uuid.uuid4())
    # Retried like every IntegrationStateManager Redis op (review finding: an
    # unretried transient Redis error here escaped as a fake per-device
    # failure downstream — a blip in OUR Redis said nothing about Lotek).
    async for attempt in stamina.retry_context(
        on=redis.RedisError, attempts=3, wait_initial=0.1, wait_max=2.0
    ):
        with attempt:
            now = time.time()
            acquired = await client.eval(
                _ACQUIRE_LUA, 1, key, now, now + ttl_seconds,
                settings.LOTEK_MAX_CONNECTIONS, token, ttl_seconds * 2,
            )
    if not acquired:
        raise NoConnectionSlot(f"No Lotek connection slot available (limit {settings.LOTEK_MAX_CONNECTIONS}).")
    try:
        yield
    finally:
        # Best-effort release: a failed zrem must not turn an otherwise-successful
        # Lotek call into an action failure (spurious retry). The slot is
        # time-bounded and purged on the next acquire, so a missed release
        # self-heals via TTL.
        try:
            await client.zrem(key, token)
        except Exception as exc:
            logger.warning(f"Failed to release Lotek connection slot (will expire via TTL): {exc}")
