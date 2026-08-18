from environs import Env

env = Env()
env.read_env()

OBSERVATIONS_BATCH_SIZE = env.int("OBSERVATIONS_BATCH_SIZE", default=200)

# Per-account ceiling on simultaneous Lotek API requests, enforced in Redis
# across all concurrently-running shard/backfill invocations (GUNDI-5620:
# shards fan out via pubsub, so an in-process semaphore cannot bound them).
# Sized to a handful of shards' worth of FETCH_CONCURRENCY without recreating
# the pool exhaustion the shared-client fix removed.
LOTEK_MAX_CONNECTIONS = env.int("LOTEK_MAX_CONNECTIONS", 20)
