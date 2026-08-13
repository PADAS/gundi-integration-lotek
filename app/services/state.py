import json
import uuid
from typing import Optional

import stamina
import httpx
import redis.asyncio as redis
from app import settings


# Compare-and-delete: only deletes the key if it still holds the caller's
# token, so a stale releaser (e.g. a cancelled run whose DEL was retried
# through a Redis blip past its own TTL) cannot delete a successor's lease.
_RELEASE_LEASE_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


class IntegrationStateManager:

    def __init__(self, **kwargs):
        host = kwargs.get("host", settings.REDIS_HOST)
        port = kwargs.get("port", settings.REDIS_PORT)
        db = kwargs.get("db", settings.REDIS_STATE_DB)
        self.db_client = redis.Redis(host=host, port=port, db=db)

    async def get_state(self, integration_id: str, action_id: str, source_id: str = "no-source") -> dict:
        for attempt in stamina.retry_context(on=redis.RedisError, attempts=5, wait_initial=1.0, wait_max=30, wait_jitter=3.0):
            with attempt:
                json_value = await self.db_client.get(f"integration_state.{integration_id}.{action_id}.{source_id}")
        value = json.loads(json_value) if json_value else {}
        return value

    async def set_state(self, integration_id: str, action_id: str, state: dict, source_id: str = "no-source"):
        for attempt in stamina.retry_context(on=redis.RedisError, attempts=5, wait_initial=1.0, wait_max=30, wait_jitter=3.0):
            with attempt:
                await self.db_client.set(
                    f"integration_state.{integration_id}.{action_id}.{source_id}",
                    json.dumps(state, default=str)
                )

    async def set_if_absent(
        self, integration_id: str, action_id: str, *, ttl_seconds: int, source_id: str = "no-source"
    ) -> bool:
        """Atomically set a key only if it does not already exist, with a TTL.

        Returns True if the key was set by this call (i.e. the caller is the
        first within the TTL window), or False if it already existed. Useful
        for rate-limiting/throttling repeated events: the first caller in each
        window gets True, the rest get False until the key expires.
        """
        for attempt in stamina.retry_context(on=redis.RedisError, attempts=5, wait_initial=1.0, wait_max=30, wait_jitter=3.0):
            with attempt:
                was_set = await self.db_client.set(
                    f"integration_state.{integration_id}.{action_id}.{source_id}",
                    "1",
                    ex=ttl_seconds,
                    nx=True,
                )
        return bool(was_set)

    async def acquire_lease(
        self, integration_id: str, action_id: str, *, ttl_seconds: int, source_id: str = "no-source"
    ) -> Optional[str]:
        """Atomically acquire an ownership lease: SET NX of a unique token with
        a TTL. Returns the token when acquired (pass it to release_lease), or
        None when another holder already has the lease. Unlike set_if_absent,
        the token lets the holder release without racing a successor that
        acquired after the TTL expired.
        """
        token = str(uuid.uuid4())
        key = f"integration_state.{integration_id}.{action_id}.{source_id}"
        for attempt in stamina.retry_context(on=redis.RedisError, attempts=5, wait_initial=1.0, wait_max=30, wait_jitter=3.0):
            with attempt:
                was_set = await self.db_client.set(key, json.dumps(token), ex=ttl_seconds, nx=True)
        return token if was_set else None

    async def release_lease(
        self, integration_id: str, action_id: str, token: str, source_id: str = "no-source"
    ) -> bool:
        """Release a lease acquired with acquire_lease, only if this caller
        still owns it (compare-and-delete on the token). Returns True when the
        lease was deleted, False when it was already expired or re-acquired by
        someone else. Safe to retry: it can never delete another holder's lease.
        """
        key = f"integration_state.{integration_id}.{action_id}.{source_id}"
        for attempt in stamina.retry_context(on=redis.RedisError, attempts=5, wait_initial=1.0, wait_max=30, wait_jitter=3.0):
            with attempt:
                deleted = await self.db_client.eval(_RELEASE_LEASE_SCRIPT, 1, key, json.dumps(token))
        return bool(deleted)

    async def delete_state(self, integration_id: str, action_id: str, source_id: str = "no-source"):
        for attempt in stamina.retry_context(on=redis.RedisError, attempts=5, wait_initial=1.0, wait_max=30, wait_jitter=3.0):
            with attempt:
                await self.db_client.delete(
                    f"integration_state.{integration_id}.{action_id}.{source_id}"
                )

    def __str__(self):
        return f"IntegrationStateManager(host={self.db_client.host}, port={self.db_client.port}, db={self.db_client.db})"

    def __repr__(self):
        return self.__str__()
