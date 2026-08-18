import hashlib
import logging
import time
import uuid
from contextlib import asynccontextmanager

import redis.asyncio as redis

from app import settings

logger = logging.getLogger(__name__)


class NoConnectionSlot(Exception):
    """Raised when the Lotek connection budget for a username is exhausted."""


# Atomic acquire: purge expired slots, then add a new slot only if under the
# ceiling. KEYS[1]=zset key. ARGV: now, expiry, ceiling, token.
# Returns 1 if acquired, 0 if at capacity.
_ACQUIRE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[1])
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
    now = time.time()
    acquired = await client.eval(
        _ACQUIRE_LUA, 1, key, now, now + ttl_seconds, settings.LOTEK_MAX_CONNECTIONS, token
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
