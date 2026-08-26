from environs import Env

env = Env()
env.read_env()

OBSERVATIONS_BATCH_SIZE = env.int("OBSERVATIONS_BATCH_SIZE", default=200)

# THE account-wide ceiling on simultaneous Lotek requests, enforced in Redis
# across all concurrently-running shard/backfill invocations (shards fan out via
# pubsub, so an in-process semaphore cannot bound them). This is the ONLY
# concurrency governor in the connector: SHARD_SIZE and FETCH_CONCURRENCY are
# work-partitioning parameters that may oversubscribe it, because lotek_slot
# queues on a saturated budget instead of refusing. Raise this to allow more
# parallelism against one Lotek account; do not tune the partitioning constants
# for that purpose.
LOTEK_MAX_CONNECTIONS = env.int("LOTEK_MAX_CONNECTIONS", 20)
