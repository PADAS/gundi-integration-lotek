import asyncio
import hashlib
import logging
import random
import time
import uuid
from contextlib import asynccontextmanager
from typing import NoReturn, Optional

import redis.asyncio as redis
import stamina

from app import settings

logger = logging.getLogger(__name__)


# The uniform Redis retry policy used by every IntegrationStateManager and
# config-manager op (app/services/state.py). Kept identical on purpose: the
# slot acquire previously used attempts=3/wait_initial=0.1/wait_max=2.0, so a
# multi-second Redis brownout that every get_state around it survived exhausted
# this budget and escaped as RedisError into the generic per-device handler —
# a fabricated Lotek failure caused by our own Redis (review finding).
SLOT_REDIS_RETRY = dict(attempts=5, wait_initial=1.0, wait_max=30, wait_jitter=3.0)

# max_wait_seconds is the SATURATION-queueing budget (spec D2/D3: don't queue
# behind a busy account), not a Redis-error budget — the two are different
# things a caller can afford. Without a floor, a fail-fast caller
# (max_wait_seconds=0, i.e. the dispatcher) got exactly one unretried acquire
# on a Redis brownout, silently losing SLOT_REDIS_RETRY entirely (review
# finding). This floors the retry window on the first acquire pass only, so
# every caller keeps the full 5-attempt policy on a brownout; the overrun is
# bounded and only happens while Redis itself is unhealthy, where the action
# is failing regardless. Value covers SLOT_REDIS_RETRY's worst case, derived
# rather than sampled (Copilot review: a 20.0 floor taken from one ~19.7s
# measurement could cut attempt 5 short on unlucky jitter): exponential waits
# between attempts 1..5 are 1+2+4+8 = 15s, plus wait_jitter up to 3s on each
# of the four waits = up to 27s of backoff, plus the eval calls themselves.
SLOT_REDIS_RETRY_FLOOR = 30.0

# Backpressure poll schedule for a saturated account budget. Jittered so that
# N shards refused in the same millisecond do not retry in lockstep.
SLOT_WAIT_POLL_INITIAL = 0.25
SLOT_WAIT_POLL_MAX = 2.0
SLOT_WAIT_JITTER = 0.25

# Socket-level bound on every op this client performs. The give-up zrem, the
# expired-window final attempt, and the release zrem all run precisely when
# Redis may be misbehaving, and redis-py's unbounded defaults
# (socket_timeout=None) let a HUNG Redis park any of them until the outer
# 540s action timeout — defeating retry_window and the caller's remaining
# budget, and converting a diagnosable NoConnectionSlot skip into a full
# action timeout through _handle_error's config_data publish (Copilot
# review, round 6). A hung op now surfaces as redis.TimeoutError, a
# RedisError subclass, so it takes the existing retry/classification paths.
SLOT_REDIS_SOCKET_TIMEOUT = 5.0


class NoConnectionSlot(Exception):
    """Raised when the Lotek connection budget for a username is exhausted."""


class SlotBackendUnavailable(NoConnectionSlot):
    """Raised when the slot could not be acquired because REDIS failed, not
    because the account is saturated. Subclasses NoConnectionSlot so a caller
    that does not distinguish the two still degrades to the safe non-raising
    capacity-deferral path — but callers that report health MUST distinguish
    them (Copilot review, round 5): during a persistent Redis outage every
    pull otherwise reports as a clean capacity deferral, `budget_starved`
    suppresses the zero-progress ERROR, and the portal health signal never
    moves while no data flows."""


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
if redis.call('ZSCORE', KEYS[1], ARGV[4]) then
    redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[5]))
    return 1
end
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[3]) then
    redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[5]))
    return 1
end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[5]))
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
            host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_STATE_DB,
            socket_timeout=SLOT_REDIS_SOCKET_TIMEOUT,
            socket_connect_timeout=SLOT_REDIS_SOCKET_TIMEOUT,
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
async def lotek_slot(username: str, *, ttl_seconds: int = 300, max_wait_seconds: float = 0.0):
    """Acquire one Lotek connection slot for `username`, shared across every
    shard/backfill invocation on the same Redis.

    With `max_wait_seconds > 0` a saturated budget makes the caller QUEUE
    (jittered poll) rather than fail: fan-out deliberately oversubscribes the
    ceiling, so refusing on first contention aborted whole shards and turned
    ordinary large-fleet ticks into zero-progress churn (spec D2). The wait is
    capped by the caller's remaining action budget, so queueing can never cause
    a timeout that failing fast would have avoided. NoConnectionSlot now means
    "saturated for longer than I can afford to wait".

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
    # One token for the whole acquire, including every wait poll: the Lua's
    # ZSCORE fast-path re-grants an already-held slot, so re-using the token is
    # what makes a lost reply safe (Task 1).
    token = str(uuid.uuid4())
    deadline = time.monotonic() + max(0.0, max_wait_seconds)
    backoff = SLOT_WAIT_POLL_INITIAL
    acquired = 0

    async def _acquire_once():
        now = time.time()
        return await client.eval(
            _ACQUIRE_LUA, 1, key, now, now + ttl_seconds,
            settings.LOTEK_MAX_CONNECTIONS, token, ttl_seconds * 2,
        )

    async def _give_up(reason, exc: Optional[BaseException] = None) -> NoReturn:
        # Shared give-up path: if the Lua actually granted this token on an
        # attempt whose reply we never saw (lost reply on the last try), no
        # one else will ever release it. Best-effort zrem so a lost-reply
        # slot isn't stranded for the full TTL; failure here must not mask
        # the NoConnectionSlot we're about to raise.
        try:
            await client.zrem(key, token)
        except Exception as cleanup_exc:
            logger.warning(
                f"Failed to release Lotek connection slot on {reason} "
                f"(will expire via TTL): {cleanup_exc}"
            )
        message = (
            f"No Lotek connection slot available within "
            f"{max_wait_seconds:.1f}s (limit {settings.LOTEK_MAX_CONNECTIONS})."
        )
        if exc is not None:
            # `exc` is the Redis-side failure that caused this give-up, when
            # there is one — genuine account saturation (server said "no
            # slot", no exception at all) passes None. Without this, a
            # Redis-caused give-up was indistinguishable from ordinary
            # saturation once it became NoConnectionSlot, and _log_deferral
            # then reported "connection budget exhausted" — an actively wrong
            # diagnosis when Redis, not Lotek's account limit, is the cause
            # (review finding).
            logger.warning(
                f"Giving up on a Lotek connection slot for {username} because "
                f"of a Redis error, not account saturation ({reason}): {exc}"
            )
            raise SlotBackendUnavailable(message) from exc
        raise NoConnectionSlot(message)

    first_pass = True
    while True:
        acquired = 0
        # max_wait_seconds is the SATURATION-queueing budget, not a Redis-error
        # budget: a fail-fast caller (max_wait_seconds=0, i.e. the dispatcher)
        # still wants D5's shared retry policy on a brownout — it just must
        # not queue behind a busy account. Floor the retry window so every
        # caller keeps the full 5 attempts; the overrun is bounded and only
        # happens on a Redis brownout, where the action is failing regardless.
        # Only the first pass gets the floor, so a caller already deep into
        # its queueing budget is not extended on every poll.
        floor = SLOT_REDIS_RETRY_FLOOR if first_pass else 0.0
        first_pass = False
        retry_window = max(deadline - time.monotonic(), floor)
        if retry_window <= 0:
            # Only reachable on a non-first pass (the first pass is floored):
            # the final saturation sleep consumed the whole queueing budget.
            # wait_for(timeout<=0) would raise TimeoutError before any Redis
            # call ran, sending ordinary saturation down the Redis-blamed
            # give-up path with a chained cause (Copilot review). The sleep
            # existed to wait for a peer to release a slot, so make one last
            # unretried attempt, then give up under the correct diagnosis.
            try:
                acquired = await _acquire_once()
            except redis.RedisError as exc:
                await _give_up("final-attempt give-up", exc)
            if acquired:
                break
            await _give_up("give-up")
        try:
            async def _bounded_retry():
                async for attempt in stamina.retry_context(on=redis.RedisError, **SLOT_REDIS_RETRY):
                    with attempt:
                        return await _acquire_once()
            acquired = await asyncio.wait_for(_bounded_retry(), timeout=retry_window)
        except (redis.RedisError, asyncio.TimeoutError) as exc:
            # Either stamina exhausted SLOT_REDIS_RETRY, or the retry/backoff
            # itself ran past retry_window before an attempt succeeded — both
            # mean Redis, not the Lotek account, is why this caller has no
            # slot.
            await _give_up("retry give-up", exc)
        if acquired:
            break
        # Saturated. Wait for a peer to release rather than aborting the
        # caller's whole tail (spec D2/D3) — but never past the budget the
        # caller can afford to spend.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            await _give_up("give-up")
        pause = min(backoff + random.uniform(0, SLOT_WAIT_JITTER), remaining)
        if pause > 0:
            await asyncio.sleep(pause)
        backoff = min(backoff * 2 or SLOT_WAIT_POLL_INITIAL, SLOT_WAIT_POLL_MAX)
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
