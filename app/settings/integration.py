from environs import Env

env = Env()
env.read_env()

OBSERVATIONS_BATCH_SIZE = env.int("OBSERVATIONS_BATCH_SIZE", default=200)

# The account-wide ceiling on simultaneous Lotek requests, enforced in Redis
# across all concurrently-running shard/backfill invocations (shards fan out via
# pubsub, so an in-process semaphore cannot bound them).
#
# This is the only CROSS-INVOCATION governor, and the only one that bounds total
# load on a Lotek account. FETCH_CONCURRENCY is also a real concurrency limit —
# a per-invocation one: DeviceTraversal gathers one coroutine per device in each
# chunk, so it caps in-flight requests within a single shard (PR #20 review
# corrected an earlier comment here that called it "not a concurrency limit",
# which was plainly wrong and could invite someone to raise it believing it were
# free). What it is NOT is the account ceiling: chunks may oversubscribe this
# value, because lotek_slot queues on a saturated budget instead of refusing.
#
# So: raise THIS to allow more parallelism against one Lotek account. Raising
# FETCH_CONCURRENCY only makes each shard queue more requests at the slot — it
# adds coroutines and pending waits, not throughput.
LOTEK_MAX_CONNECTIONS = env.int("LOTEK_MAX_CONNECTIONS", 20)
