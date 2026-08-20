# Lotek Rail Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the scaffolding left by PRs #14–#20 — one concurrency governor instead of three overlapping ones, one shared device traversal instead of two ~270-line copies — while fixing the composition bugs that layering produced.

**Architecture:** Per the approved spec `docs/superpowers/specs/2026-08-20-lotek-consolidation-design.md` (source of truth). `lotek_slot` becomes a bounded, deadline-aware wait so the Redis connection budget applies *backpressure* instead of *refusal*, making `LOTEK_MAX_CONNECTIONS` the only concurrency limit and demoting `FETCH_CONCURRENCY` / `SHARD_SIZE` to work-partitioning parameters. A new `DeviceTraversal` helper owns the chunked-loop mechanics shared by the head pass and the backfill; policy (re-trigger, deferral wording, zero-progress) stays in each handler.

**Tech Stack:** Python 3.11, pydantic v1, httpx, stamina, redis.asyncio, pytest + pytest-asyncio + pytest-mock. Suite runs with `./venv/bin/python -m pytest app -q`.

**Spec:** `docs/superpowers/specs/2026-08-20-lotek-consolidation-design.md`

## Global Constraints

- **Branch from PR #20's head, not `main`.** `main` is six PRs behind. Base this work on `feat/GUNDI-5620-sharded-head-pass`; if PR #20 has merged by the time you start, rebase onto `main` and re-run the suite before Task 1.
- TDD strictly: failing test first, then minimal implementation. For tests that pin a specific production line, flip-verify: change the line, watch the test fail, restore byte-identical.
- Test suite must stay **< 2 s**. Patch `RETRY_WAIT_INITIAL` / `RETRY_WAIT_JITTER` / `RETRY_WAIT_MAX` and the new `SLOT_WAIT_*` constants to 0 in any test exercising retries or waiting. **Never `sleep`.**
- **Behaviour-preserving except where the spec says otherwise.** The only intended behaviour changes are: slot waits (D2), starvation defers narrowly (D3), backfill zero-progress stops raising (D7), and the claim rolls back (D8). Observation output must be unchanged.
- Health/alerting keys on ERROR-count only (cdip `calculate_integration_status`, ≥3 ERROR logs / 60 min → UNHEALTHY). ERROR = "someone can and should act". WARNING = transient per-device failures and deferrals. Do not add new ERROR paths.
- `Optional[str]`, not `str | None` (pydantic v1 codebase style). All datetimes tz-aware UTC.
- Code constants, NOT config fields, for anything added here.
- Commit after every green task. One task = one commit.
- Do **not** touch `app/services/config_manager.py` or `app/services/action_runner.py` — those gaps are tracked in GUNDI-5628.

---

### Task 1: Make the slot acquire idempotent

Prerequisite for Task 3: waiting multiplies acquire attempts, which widens the lost-reply window this fixes.

**Files:**
- Modify: `app/services/lotek_connections.py` (the `_ACQUIRE_LUA` script, ~lines 24-36)
- Test: `app/services/tests/test_lotek_connections.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `_ACQUIRE_LUA` gains a `ZSCORE` membership fast-path. ARGV order is **unchanged**: `now, expiry, ceiling, token, key_ttl`. Existing tests index `args[-2]` for the token and must keep passing.

- [ ] **Step 1: Write the failing test**

Add to `app/services/tests/test_lotek_connections.py`:

```python
@pytest.mark.asyncio
async def test_reacquire_with_same_token_is_idempotent(fake_redis):
    """A lost reply on the acquire that took the last slot must not refuse the
    caller that already owns that slot. The Lua checks ZSCORE for the token
    before the ZCARD ceiling test, so a retry with the same token re-grants.

    Simulated at the script level: the real guarantee lives in the Lua, so this
    test pins that the script text contains the membership fast-path ahead of
    the capacity check.
    """
    from app.services.lotek_connections import _ACQUIRE_LUA

    zscore_at = _ACQUIRE_LUA.find("ZSCORE")
    zcard_at = _ACQUIRE_LUA.find("ZCARD")
    assert zscore_at != -1, "acquire must check token membership before capacity"
    assert zscore_at < zcard_at, "membership fast-path must precede the ceiling test"
    # The fast-path must re-arm the member's expiry rather than returning a
    # stale score, so a waiting retry cannot inherit an about-to-expire slot.
    assert "ZADD" in _ACQUIRE_LUA[zscore_at:zcard_at]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest app/services/tests/test_lotek_connections.py::test_reacquire_with_same_token_is_idempotent -v`
Expected: FAIL — `AssertionError: acquire must check token membership before capacity`

- [ ] **Step 3: Write minimal implementation**

Replace `_ACQUIRE_LUA` in `app/services/lotek_connections.py`:

```python
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
```

- [ ] **Step 4: Run the file's tests to verify all pass**

Run: `./venv/bin/python -m pytest app/services/tests/test_lotek_connections.py -v`
Expected: PASS — the new test plus all 7 pre-existing ones (the ARGV order is unchanged, so `args[-2]` still resolves to the token).

- [ ] **Step 5: Commit**

```bash
git add app/services/lotek_connections.py app/services/tests/test_lotek_connections.py
git commit -m "fix: make the Lotek slot acquire idempotent for a retried token"
```

---

### Task 2: Adopt the codebase's shared Redis retry policy

**Files:**
- Modify: `app/services/lotek_connections.py` (the `stamina.retry_context` call, ~line 95)
- Test: `app/services/tests/test_lotek_connections.py`

**Interfaces:**
- Consumes: Task 1's `_ACQUIRE_LUA`
- Produces: module constant `SLOT_REDIS_RETRY = dict(attempts=5, wait_initial=1.0, wait_max=30, wait_jitter=3.0)`, importable by tests to patch.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_slot_retry_policy_matches_state_manager(fake_redis):
    """A Redis brownout that every IntegrationStateManager op survives must not
    escape from the slot acquire as a fake per-device Lotek failure. The policy
    here has to match state.py's, not undercut it.
    """
    from app.services.lotek_connections import SLOT_REDIS_RETRY

    assert SLOT_REDIS_RETRY == {
        "attempts": 5, "wait_initial": 1.0, "wait_max": 30, "wait_jitter": 3.0,
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest app/services/tests/test_lotek_connections.py::test_slot_retry_policy_matches_state_manager -v`
Expected: FAIL — `ImportError: cannot import name 'SLOT_REDIS_RETRY'`

- [ ] **Step 3: Write minimal implementation**

In `app/services/lotek_connections.py`, add near the top after `logger`:

```python
# The uniform Redis retry policy used by every IntegrationStateManager and
# config-manager op (app/services/state.py). Kept identical on purpose: the
# slot acquire previously used attempts=3/wait_initial=0.1/wait_max=2.0, so a
# multi-second Redis brownout that every get_state around it survived exhausted
# this budget and escaped as RedisError into the generic per-device handler —
# a fabricated Lotek failure caused by our own Redis (review finding).
SLOT_REDIS_RETRY = dict(attempts=5, wait_initial=1.0, wait_max=30, wait_jitter=3.0)
```

Then replace the retry call inside `lotek_slot`:

```python
    async for attempt in stamina.retry_context(on=redis.RedisError, **SLOT_REDIS_RETRY):
```

- [ ] **Step 4: Run the file's tests to verify all pass**

Run: `./venv/bin/python -m pytest app/services/tests/test_lotek_connections.py -v`
Expected: PASS (all tests; none of them exercise a RedisError retry path, so no timing change).

- [ ] **Step 5: Commit**

```bash
git add app/services/lotek_connections.py app/services/tests/test_lotek_connections.py
git commit -m "fix: put the slot acquire on the shared Redis retry policy"
```

---

### Task 3: Make the slot wait for capacity instead of refusing

This is the fix for the PR #20 review's top finding. Read spec section **D2** before starting.

**Files:**
- Modify: `app/services/lotek_connections.py` (`lotek_slot`, ~lines 68-119)
- Modify: `app/actions/handlers.py` (the `lotek_slot` call in `_fetch_window` ~line 929, and in `action_backfill_observations`' device listing ~line 1225)
- Test: `app/services/tests/test_lotek_connections.py`

**Interfaces:**
- Consumes: `SLOT_REDIS_RETRY` (Task 2), idempotent `_ACQUIRE_LUA` (Task 1)
- Produces:
  - `lotek_slot(username, *, ttl_seconds=300, max_wait_seconds=0.0)` — async context manager. When `max_wait_seconds > 0`, polls the acquire until a slot frees or the budget elapses, then raises `NoConnectionSlot`. `max_wait_seconds=0.0` preserves today's fail-fast behaviour (used by callers with no deadline to reason about).
  - Module constants `SLOT_WAIT_POLL_INITIAL = 0.25`, `SLOT_WAIT_POLL_MAX = 2.0`, `SLOT_WAIT_JITTER = 0.25`.
  - `handlers.py` gains `def _slot_wait_budget(run_started_at) -> float`, returning the seconds left before the run deadline, floored at 0.0.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_slot_waits_then_acquires_when_capacity_frees(fake_redis, monkeypatch):
    """Oversubscription must queue, not refuse: with shards x FETCH_CONCURRENCY
    well above the ceiling, a caller that waits a moment gets a slot instead of
    deferring its whole tail (spec D2)."""
    monkeypatch.setattr(lc, "SLOT_WAIT_POLL_INITIAL", 0)
    monkeypatch.setattr(lc, "SLOT_WAIT_POLL_MAX", 0)
    monkeypatch.setattr(lc, "SLOT_WAIT_JITTER", 0)
    # Refused twice (account saturated), then a peer releases.
    fake_redis.eval = AsyncMock(side_effect=[0, 0, 1])

    async with lotek_slot("user@example.com", max_wait_seconds=5.0):
        pass

    assert fake_redis.eval.await_count == 3
    fake_redis.zrem.assert_awaited_once()


@pytest.mark.asyncio
async def test_slot_gives_up_when_wait_budget_exhausted(fake_redis, monkeypatch):
    """Waiting must never eat the caller's action deadline: once the budget is
    spent the slot raises, and the caller defers as before."""
    monkeypatch.setattr(lc, "SLOT_WAIT_POLL_INITIAL", 0)
    monkeypatch.setattr(lc, "SLOT_WAIT_POLL_MAX", 0)
    monkeypatch.setattr(lc, "SLOT_WAIT_JITTER", 0)
    fake_redis.eval = AsyncMock(return_value=0)

    with pytest.raises(NoConnectionSlot):
        async with lotek_slot("user@example.com", max_wait_seconds=0.05):
            pytest.fail("body must not run when the account stays saturated")

    assert fake_redis.eval.await_count >= 1
    fake_redis.zrem.assert_not_awaited()


@pytest.mark.asyncio
async def test_slot_without_wait_budget_still_fails_fast(fake_redis):
    """Default stays fail-fast for callers with no deadline to reason about."""
    fake_redis.eval = AsyncMock(return_value=0)
    with pytest.raises(NoConnectionSlot):
        async with lotek_slot("user@example.com"):
            pytest.fail("body must not run")
    assert fake_redis.eval.await_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/python -m pytest app/services/tests/test_lotek_connections.py -k "wait" -v`
Expected: FAIL — `TypeError: lotek_slot() got an unexpected keyword argument 'max_wait_seconds'`

- [ ] **Step 3: Write minimal implementation**

In `app/services/lotek_connections.py`, add the constants next to `SLOT_REDIS_RETRY`:

```python
# Backpressure poll schedule for a saturated account budget. Jittered so that
# N shards refused in the same millisecond do not retry in lockstep.
SLOT_WAIT_POLL_INITIAL = 0.25
SLOT_WAIT_POLL_MAX = 2.0
SLOT_WAIT_JITTER = 0.25
```

Add `import asyncio` and `import random` to the imports. Replace the acquire section of `lotek_slot` (everything from `client = _client()` down to and including the `if not acquired: raise ...`) with:

```python
    client = _client()
    key = connection_key(username)
    # One token for the whole acquire, including every wait poll: the Lua's
    # ZSCORE fast-path re-grants an already-held slot, so re-using the token is
    # what makes a lost reply safe (Task 1).
    token = str(uuid.uuid4())
    deadline = time.monotonic() + max(0.0, max_wait_seconds)
    backoff = SLOT_WAIT_POLL_INITIAL
    acquired = 0
    while True:
        async for attempt in stamina.retry_context(on=redis.RedisError, **SLOT_REDIS_RETRY):
            with attempt:
                now = time.time()
                acquired = await client.eval(
                    _ACQUIRE_LUA, 1, key, now, now + ttl_seconds,
                    settings.LOTEK_MAX_CONNECTIONS, token, ttl_seconds * 2,
                )
        if acquired:
            break
        # Saturated. Wait for a peer to release rather than aborting the
        # caller's whole tail (spec D2/D3) — but never past the budget the
        # caller can afford to spend.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NoConnectionSlot(
                f"No Lotek connection slot available within "
                f"{max_wait_seconds:.1f}s (limit {settings.LOTEK_MAX_CONNECTIONS})."
            )
        pause = min(backoff + random.uniform(0, SLOT_WAIT_JITTER), remaining)
        if pause > 0:
            await asyncio.sleep(pause)
        backoff = min(backoff * 2 or SLOT_WAIT_POLL_INITIAL, SLOT_WAIT_POLL_MAX)
```

Update the docstring's first paragraph to describe waiting:

```python
    """Acquire one Lotek connection slot for `username`, shared across every
    shard/backfill invocation on the same Redis.

    With `max_wait_seconds > 0` a saturated budget makes the caller QUEUE
    (jittered poll) rather than fail: fan-out deliberately oversubscribes the
    ceiling, so refusing on first contention aborted whole shards and turned
    ordinary large-fleet ticks into zero-progress churn (spec D2). The wait is
    capped by the caller's remaining action budget, so queueing can never cause
    a timeout that failing fast would have avoided. NoConnectionSlot now means
    "saturated for longer than I can afford to wait".
    """
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest app/services/tests/test_lotek_connections.py -v`
Expected: PASS (all, including the three new ones).

- [ ] **Step 5: Pass the deadline in from both call sites**

In `app/actions/handlers.py`, add next to `_deadline_exceeded`:

```python
def _slot_wait_budget(run_started_at):
    """Seconds this run can still afford to spend queueing for a connection
    slot. Mirrors _deadline_exceeded's fraction so waiting stops exactly when
    the traversal would have stopped anyway."""
    elapsed = (datetime.now(tz=timezone.utc) - run_started_at).total_seconds()
    return max(0.0, DEADLINE_FRACTION * app_settings.MAX_ACTION_EXECUTION_TIME - elapsed)
```

In `_fetch_window`, change the slot acquire to pass the budget:

```python
                async with lotek_slot(
                    auth.username,
                    max_wait_seconds=_slot_wait_budget(guards.run_started_at),
                ):
                    positions = await client.get_positions(device_id, auth, integration, lower_date, upper_date, True)
```

In `action_backfill_observations`' device-listing block, do the same:

```python
                    async with lotek_slot(
                        auth.username, max_wait_seconds=_slot_wait_budget(run_started)
                    ):
                        device_list = await client.get_devices(integration, auth)
```

- [ ] **Step 6: Run the full suite**

Run: `./venv/bin/python -m pytest app -q`
Expected: PASS. If any existing test asserted immediate `NoConnectionSlot` from a handler path, it now needs `max_wait_seconds` patched to 0 or the `lotek_slot` mock unchanged — fix by patching `lc.SLOT_WAIT_POLL_INITIAL`/`_MAX`/`SLOT_WAIT_JITTER` to 0, never by adding a sleep.

- [ ] **Step 7: Commit**

```bash
git add app/services/lotek_connections.py app/services/tests/test_lotek_connections.py app/actions/handlers.py
git commit -m "fix: make the connection budget apply backpressure instead of refusing"
```

---

### Task 4: Defer only the devices that actually starved

**Files:**
- Modify: `app/actions/handlers.py` (shard starvation block ~lines 696-717; backfill starvation block ~lines 1337-1350)
- Test: `app/actions/tests/test_sharding.py`

**Interfaces:**
- Consumes: `lotek_slot` waiting behaviour (Task 3)
- Produces: no new symbols. Both starvation blocks stop appending the un-processed tail; the loop `continue`s instead of `break`ing.

- [ ] **Step 1: Write the failing test**

Add to `app/actions/tests/test_sharding.py`:

```python
@pytest.mark.asyncio
async def test_starved_device_does_not_abort_the_whole_shard(
    mocker, mock_gundi_client_v2, mock_state_manager, lotek_devices_response
):
    """One device losing the slot race must defer THAT device, not the shard's
    entire remaining tail (spec D3). Its in-chunk peers keep their results and
    the loop carries on to later chunks."""
    from app.actions.handlers import action_pull_observations_shard
    from app.actions.configurations import PullObservationsShardConfig
    from app.services.lotek_connections import NoConnectionSlot

    devices = [f"dev{i}" for i in range(10)]
    calls = []

    async def fake_head_pass(device_id, *args, **kwargs):
        calls.append(device_id)
        if device_id == "dev0":
            raise NoConnectionSlot("saturated")
        return (5, False, False)

    mocker.patch("app.actions.handlers._head_pass_device", side_effect=fake_head_pass)
    mocker.patch("app.actions.handlers._retrigger_shard", return_value="handed_off")

    result = await action_pull_observations_shard(
        mock_gundi_client_v2.get_integration_details.return_value,
        PullObservationsShardConfig(devices=devices),
    )

    # Every device was attempted, not just the first chunk.
    assert len(calls) == 10
    # Only the starved one is deferred.
    assert result["devices_deferred"] == ["dev0"]
    # The other nine still delivered.
    assert result["observations_extracted"] == 45
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest app/actions/tests/test_sharding.py::test_starved_device_does_not_abort_the_whole_shard -v`
Expected: FAIL — `assert len(calls) == 10` gets 5 (the shard broke after the first chunk), and `devices_deferred` contains the whole tail.

- [ ] **Step 3: Write minimal implementation**

In `action_pull_observations_shard`, replace the `if slot_starved:` block with:

```python
        if slot_starved:
            # Defer ONLY the devices that lost the slot race — their in-chunk
            # peers keep their results and later chunks still run (spec D3).
            # Before this, one starved device aborted the shard's entire tail,
            # which on an oversubscribed fan-out made mass deferral the normal
            # outcome rather than an exceptional one (review finding).
            deferred_devices.extend(slot_starved)
            budget_starved = True
```

Apply the same change in `action_backfill_observations`:

```python
            if slot_starved:
                # Defer only the starved devices (spec D3); see the head pass.
                deferred_devices.extend(slot_starved)
                budget_starved = True
```

Then move the deferral *log* out of the loop, so a run reports its starvation once. Immediately after the `for chunk_start in ...` loop ends in each handler, add:

```python
    if budget_starved:
        await _log_deferral(
            integration, "pull_observations_shard", "connection budget exhausted",
            deferred_devices, disposition="to the next scheduled run",
        )
```

(For the backfill use `action_id="backfill_observations"` and `disposition="to the next backfill trigger"`.)

Note: `deferred_devices` must be initialised to `[]` before the loop in both handlers — it already is.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest app/actions/tests/test_sharding.py app/actions/tests/test_backfill.py -v`
Expected: PASS. Existing tests that asserted a starved shard re-triggered its tail will now fail — that is the intended behaviour change; update them to assert narrow deferral and delete the re-trigger assertion.

- [ ] **Step 5: Commit**

```bash
git add app/actions/handlers.py app/actions/tests/test_sharding.py app/actions/tests/test_backfill.py
git commit -m "fix: defer only slot-starved devices instead of the whole tail"
```

---

### Task 5: Document and pin the single-governor model

**Files:**
- Modify: `app/actions/handlers.py` (constants block ~lines 41-90)
- Modify: `app/settings/integration.py` (the `LOTEK_MAX_CONNECTIONS` comment)
- Test: `app/actions/tests/test_sharding.py`

**Interfaces:**
- Consumes: Tasks 3-4
- Produces: no new symbols — comments plus one regression test.

- [ ] **Step 1: Write the failing test**

```python
def test_partitioning_constants_may_oversubscribe_the_budget():
    """Guards the spec-D1 contract: SHARD_SIZE and FETCH_CONCURRENCY are
    work-partitioning parameters, NOT concurrency limits, so they are ALLOWED
    to exceed the account budget — the budget applies backpressure (Task 3).

    This test exists so nobody 'fixes' the arithmetic by shrinking the
    partitioning constants: that would reintroduce the coupling spec D1
    removed. If you need to bound concurrency, change LOTEK_MAX_CONNECTIONS.
    """
    from app.actions.handlers import FETCH_CONCURRENCY, SHARD_SIZE
    from app.services.lotek_connections import lotek_slot
    import inspect

    assert FETCH_CONCURRENCY > 0 and SHARD_SIZE > 0
    # The budget must be a WAITING primitive for oversubscription to be safe.
    assert "max_wait_seconds" in inspect.signature(lotek_slot).parameters
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest app/actions/tests/test_sharding.py::test_partitioning_constants_may_oversubscribe_the_budget -v`
Expected: PASS if Task 3 landed. **If it passes immediately, that is correct** — this is a regression guard, not a red-green cycle. Confirm it is meaningful by flip-verifying: temporarily rename `max_wait_seconds`, watch it fail, restore.

- [ ] **Step 3: Write the documentation**

In `app/actions/handlers.py`, replace the comments above `FETCH_CONCURRENCY` and `SHARD_SIZE`:

```python
# How many devices one invocation processes per chunk. WORK PARTITIONING, not a
# concurrency limit: the account-wide ceiling is LOTEK_MAX_CONNECTIONS, enforced
# in Redis by lotek_slot, which now WAITS rather than refusing. Chunk size may
# freely oversubscribe that ceiling — the budget applies backpressure (spec D1).
FETCH_CONCURRENCY = 5
```

```python
# How much work fits in one action budget, i.e. how the dispatcher partitions
# the fleet across sub-actions. WORK PARTITIONING, not a concurrency limit —
# see FETCH_CONCURRENCY. Do not shrink this to "fit" LOTEK_MAX_CONNECTIONS;
# that reintroduces the coupling spec D1 removed.
SHARD_SIZE = 25
```

In `app/settings/integration.py`, replace the `LOTEK_MAX_CONNECTIONS` comment:

```python
# THE account-wide ceiling on simultaneous Lotek requests, enforced in Redis
# across all concurrently-running shard/backfill invocations (shards fan out via
# pubsub, so an in-process semaphore cannot bound them). This is the ONLY
# concurrency governor in the connector: SHARD_SIZE and FETCH_CONCURRENCY are
# work-partitioning parameters that may oversubscribe it, because lotek_slot
# queues on a saturated budget instead of refusing. Raise this to allow more
# parallelism against one Lotek account; do not tune the partitioning constants
# for that purpose.
LOTEK_MAX_CONNECTIONS = env.int("LOTEK_MAX_CONNECTIONS", 20)
```

- [ ] **Step 4: Run the full suite**

Run: `./venv/bin/python -m pytest app -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/actions/handlers.py app/settings/integration.py app/actions/tests/test_sharding.py
git commit -m "docs: name the single concurrency governor and pin the contract"
```

---

### Task 6: Extract `DeviceTraversal` and convert the head pass to it

The core consolidation. Read spec section **D6** before starting — the seam (mechanics in the traversal, policy in the caller) is the whole point, and blurring it produces a worse file than the duplication did.

**Files:**
- Create: `app/actions/traversal.py`
- Modify: `app/actions/core.py` (gains `describe_exception`, moved in Step 3)
- Modify: `app/actions/handlers.py` (`action_pull_observations_shard`, ~lines 601-717; `describe_exception` moves out)
- Test: `app/actions/tests/test_traversal.py`

**Interfaces:**
- Consumes: `RunGuards` and `_log_deferral` from `handlers.py`. To avoid a circular import, `DeviceTraversal` takes the already-built `guards` object and does **no** logging of deferrals itself — it only records what happened.
- Produces:

```python
class DeviceTraversal:
    """Shared chunked-device-loop mechanics for the head pass and the backfill.

    Owns: chunking, gather-with-return_exceptions, fatal re-raise, per-device
    failure logging, slot-starvation collection, guard-stop detection.
    Does NOT own: re-trigger decisions, deferral wording, zero-progress policy.
    """
    def __init__(self, integration, action_id, guards, *, concurrency): ...

    async def run(self, work, key, process):
        """Async-iterates (item, value) for every device that produced a result.
        `key(item) -> device_id` (logging/deferral ids), `process(item) -> awaitable`."""

    # Populated during/after run():
    failed_devices: list      # per-device failures, already logged at ERROR
    deferred_devices: list    # starved devices + un-reached tail on a guard stop
    serviced_devices: int     # results yielded that the caller did not mark failed
    stop_reason: Optional[str]  # None | "deadline" | "circuit breaker"
    budget_starved: bool      # at least one device lost the slot race

    # NOTE: there is deliberately NO `deferred_cleanly` here. Whether a given
    # stop counts as "cleanly deferred" is POLICY and the two callers disagree:
    # the shard suppresses its zero-progress alert on a successful hand-off, a
    # cap-reached deferral, or starvation, but NOT on a breaker stop; the
    # backfill suppresses on starvation only. Hoisting it here would silently
    # change both handlers' alerting. Each caller computes its own (spec D6).
```

- `serviced_devices` cannot be counted by the traversal (only the caller knows whether a yielded result means success), so `run()` exposes `mark_failed(device_id)` for the caller to call inside the loop; `serviced_devices` is derived as `yielded - len(failed_devices)`.

- [ ] **Step 1: Write the failing tests**

Create `app/actions/tests/test_traversal.py`:

```python
import asyncio
import pytest
from unittest.mock import AsyncMock

from gundi_core.events import LogLevel

from app.actions.traversal import DeviceTraversal
from app.actions.client import LotekUnauthorizedException
from app.services.lotek_connections import NoConnectionSlot


class FakeGuards:
    """RunGuards stand-in: stops when told, records transport failures."""
    def __init__(self, stop_after=None):
        self.stop_after = stop_after
        self.calls = 0
        self.recorded = []
    def should_stop(self):
        self.calls += 1
        if self.stop_after is not None and self.calls > self.stop_after:
            return "deadline"
        return None
    def record(self, transport_failure):
        self.recorded.append(transport_failure)


@pytest.fixture
def integration(mocker):
    return mocker.Mock(id="11111111-1111-1111-1111-111111111111")


@pytest.mark.asyncio
async def test_yields_every_successful_result(integration):
    t = DeviceTraversal(integration, "act", FakeGuards(), concurrency=2)
    seen = []
    async for item, value in t.run([1, 2, 3], key=str, process=lambda i: _ok(i)):
        seen.append((item, value))
    assert seen == [(1, "r1"), (2, "r2"), (3, "r3")]
    assert t.failed_devices == []
    assert t.serviced_devices == 3


async def _ok(i):
    return f"r{i}"


@pytest.mark.asyncio
async def test_per_device_failure_is_isolated_and_logged(integration, mocker):
    log = mocker.patch("app.actions.traversal.log_action_activity", new=AsyncMock())

    async def process(i):
        if i == 2:
            raise ValueError("boom")
        return f"r{i}"

    t = DeviceTraversal(integration, "act", FakeGuards(), concurrency=3)
    seen = [item async for item, _ in t.run([1, 2, 3], key=str, process=process)]

    assert seen == [1, 3]              # device 2 did not stop its peers
    assert t.failed_devices == ["2"]
    assert log.await_count == 1
    assert log.await_args.kwargs["level"] is LogLevel.ERROR


@pytest.mark.asyncio
async def test_unauthorized_propagates(integration):
    async def process(i):
        raise LotekUnauthorizedException("refused")

    t = DeviceTraversal(integration, "act", FakeGuards())
    with pytest.raises(LotekUnauthorizedException):
        async for _ in t.run([1], key=str, process=process):
            pytest.fail("must not yield")


@pytest.mark.asyncio
async def test_cancellation_propagates(integration):
    async def process(i):
        raise asyncio.CancelledError()

    t = DeviceTraversal(integration, "act", FakeGuards())
    with pytest.raises(asyncio.CancelledError):
        async for _ in t.run([1], key=str, process=process):
            pytest.fail("must not yield")


@pytest.mark.asyncio
async def test_slot_starvation_records_narrowly(integration):
    async def process(i):
        if i == 1:
            raise NoConnectionSlot("saturated")
        return f"r{i}"

    t = DeviceTraversal(integration, "act", FakeGuards(), concurrency=3)
    seen = [item async for item, _ in t.run([1, 2, 3], key=str, process=process)]

    assert seen == [2, 3]                 # peers unaffected (spec D3)
    assert t.deferred_devices == ["1"]
    assert t.budget_starved is True
    assert t.failed_devices == []         # starvation is not a device failure


@pytest.mark.asyncio
async def test_guard_stop_defers_the_unreached_tail(integration):
    t = DeviceTraversal(integration, "act", FakeGuards(stop_after=1), concurrency=2)
    seen = [item async for item, _ in t.run([1, 2, 3, 4], key=str, process=_ok)]

    assert seen == [1, 2]                 # first chunk only
    assert t.stop_reason == "deadline"
    assert t.deferred_devices == ["3", "4"]


@pytest.mark.asyncio
async def test_clean_completion_records_no_stop_and_no_starvation(integration):
    t = DeviceTraversal(integration, "act", FakeGuards(), concurrency=2)
    _ = [item async for item, _ in t.run([1], key=str, process=_ok)]
    assert t.stop_reason is None
    assert t.budget_starved is False
    assert t.deferred_devices == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/python -m pytest app/actions/tests/test_traversal.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.actions.traversal'`

- [ ] **Step 3: Move `describe_exception` to the shared module**

`traversal.py` needs it too, and copying it would duplicate a helper in the very
PR that exists to remove duplication — with a subtle behaviour change, since the
naive `f"{type(exc).__name__}: {exc}"` form loses the empty-message handling that
comment in `handlers.py` exists to explain.

`app/actions/core.py` is the right home: `handlers.py` already imports from it,
and `core.py` only imports `app.services.utils` at module scope (its one
`handlers` reference is a lazy `importlib` call inside `discover_actions`), so
there is no circular import.

Cut this function out of `app/actions/handlers.py` and paste it into
`app/actions/core.py`, byte-identical:

```python
def describe_exception(exc):
    # httpx timeout exceptions carry an empty message, which used to render as a bare
    # "Exception: " in the activity log and told operators nothing.
    return str(exc) or type(exc).__name__
```

Then add it to `handlers.py`'s existing import from that module:

```python
from app.actions.core import describe_exception
```

Run: `./venv/bin/python -m pytest app -q`
Expected: PASS — a pure move, no behaviour change.

- [ ] **Step 4: Write minimal implementation**

Create `app/actions/traversal.py`:

```python
import asyncio
import logging
from typing import Optional

from gundi_core.events import LogLevel

from app.actions.client import LotekUnauthorizedException
from app.actions.core import describe_exception
from app.services.activity_logger import log_action_activity
from app.services.lotek_connections import NoConnectionSlot

logger = logging.getLogger(__name__)


class DeviceTraversal:
    """Shared chunked-device-loop mechanics for the head pass and the backfill.

    Owns: chunking, gather-with-return_exceptions, fatal re-raise, per-device
    failure logging, slot-starvation collection, guard-stop detection, and the
    deferred-tail computation.

    Deliberately does NOT own: whether to re-trigger, what the deferral message
    says, or what zero progress means. Those differ between the head pass and
    the backfill and stay in the handlers (spec D6). Keeping policy out is what
    makes one traversal serve both without a flag soup.
    """

    def __init__(self, integration, action_id, guards, *, concurrency):
        self.integration = integration
        self.integration_id = str(integration.id)
        self.action_id = action_id
        self.guards = guards
        self.concurrency = concurrency
        self.failed_devices = []
        self.deferred_devices = []
        self.stop_reason: Optional[str] = None
        self.budget_starved = False
        self._yielded = 0

    @property
    def serviced_devices(self):
        # Only the caller knows whether a yielded result counts as success, so
        # it calls mark_failed() for the ones that don't.
        return self._yielded - len(self.failed_devices)

    def mark_failed(self, device_id):
        """Caller-side failure: the device produced a result, but the result
        says it failed (e.g. delivery rejected)."""
        self.failed_devices.append(device_id)

    async def run(self, work, key, process):
        work = list(work)
        for chunk_start in range(0, len(work), self.concurrency):
            if reason := self.guards.should_stop():
                self.stop_reason = reason
                self.deferred_devices.extend(key(item) for item in work[chunk_start:])
                return
            chunk = work[chunk_start:chunk_start + self.concurrency]
            results = await asyncio.gather(
                *(process(item) for item in chunk),
                # Collect every task's outcome rather than aborting the chunk on
                # the first exception: per-device failures must stay per-device.
                return_exceptions=True,
            )
            # Credentials refused is integration-wide and fatal; re-raise it over
            # any per-device outcomes in the same chunk. Cancellation must also
            # propagate: with return_exceptions=True a task's CancelledError
            # comes back as a result, and treating it as a device failure would
            # swallow shutdown/timeout cancellation and keep the run going.
            for res in results:
                if isinstance(res, (LotekUnauthorizedException, asyncio.CancelledError)):
                    raise res
            for item, res in zip(chunk, results):
                device_id = key(item)
                if isinstance(res, NoConnectionSlot):
                    # Account budget saturated for longer than this run can wait:
                    # not a device failure and not evidence about Lotek. Defer
                    # THIS device only; peers and later chunks continue (D3).
                    self.deferred_devices.append(device_id)
                    self.budget_starved = True
                    continue
                if isinstance(res, BaseException):
                    message = (
                        f"Failed to process device {device_id} for integration "
                        f"{self.integration.id}: {describe_exception(res)}"
                    )
                    logger.error(message, exc_info=res)
                    await log_action_activity(
                        integration_id=self.integration_id,
                        action_id=self.action_id,
                        title=message,
                        level=LogLevel.ERROR,
                    )
                    self.failed_devices.append(device_id)
                    self.guards.record(transport_failure=False)
                    continue
                self._yielded += 1
                yield item, res
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest app/actions/tests/test_traversal.py -v`
Expected: PASS (all 7)

- [ ] **Step 6: Convert the head pass to use it**

In `app/actions/handlers.py`, add the import:

```python
from app.actions.traversal import DeviceTraversal
```

Replace the whole loop body in `action_pull_observations_shard` — from `guards = RunGuards(run_started)` through the end of the `for chunk_start in ...` loop — with:

```python
    guards = RunGuards(run_started)
    traversal = DeviceTraversal(
        integration, "pull_observations_shard", guards, concurrency=FETCH_CONCURRENCY
    )
    observations_extracted = 0
    stale_drops = []
    # Only reflects devices actually processed this run — a device deferred by
    # the rails before its gap status is checked doesn't trigger backfill this
    # cycle. Self-correcting: the re-triggered tail (or the next tick) reaches it.
    any_open_gap = False

    async for (device_id, state, is_new), res in traversal.run(
        device_states,
        key=lambda entry: entry[0],
        process=lambda entry: _head_pass_device(
            entry[0], entry[1], entry[2], integration, auth, pull_config,
            present_time, guards, stale_drops=stale_drops,
        ),
    ):
        sent, device_failed, transport_failure = res
        # Single recording site: transport failures arm the breaker, anything
        # else (success included) breaks the consecutive streak.
        guards.record(transport_failure=transport_failure)
        observations_extracted += sent
        if state.has_gap:
            any_open_gap = True
        if device_failed:
            traversal.mark_failed(device_id)

    failed_devices = traversal.failed_devices
    deferred_devices = traversal.deferred_devices

    # Policy, deliberately NOT in the traversal (spec D6): a deadline cut gets a
    # fresh budget immediately; a hot breaker does NOT re-trigger — that would
    # defeat the pause the breaker exists to buy. Its devices wait for the next
    # scheduled tick.
    retrigger_outcome = None
    if traversal.stop_reason == "deadline" and deferred_devices:
        retrigger_outcome = await _retrigger_shard(
            integration, deferred_devices, action_config.generation,
            manual_run=action_config.manual_run,
        )
    if traversal.stop_reason:
        await _log_deferral(
            integration, "pull_observations_shard", traversal.stop_reason,
            deferred_devices,
            disposition=(
                "to an immediately re-triggered shard"
                if retrigger_outcome == RETRIGGER_HANDED_OFF
                else "to the next scheduled run"
            ),
        )
    if traversal.budget_starved:
        # Portal WARNING like every other deferral cause: a starved tail must
        # not park with zero portal visibility (review finding).
        await _log_deferral(
            integration, "pull_observations_shard", "connection budget exhausted",
            deferred_devices, disposition="to the next scheduled run",
        )
```

Then replace the `zero_progress` computation with the traversal-backed form:

```python
    # Suppression policy, preserved EXACTLY as it was before the traversal
    # existed: a successful hand-off, a cap-reached deferral (which already
    # alerted at ERROR inside _retrigger_shard), or slot starvation all explain
    # the lack of progress. A BREAKER stop deliberately does not — a Lotek-wide
    # outage must still raise the zero-progress ERROR that the cdip health
    # metric counts.
    deferred_cleanly = (
        retrigger_outcome in (RETRIGGER_HANDED_OFF, RETRIGGER_CAP_REACHED)
        or traversal.budget_starved
    )
    zero_progress = (
        device_states and traversal.serviced_devices == 0
        and observations_extracted == 0 and not deferred_cleanly
    )
```

Delete the now-unused `retriggered`, `budget_starved`, `cap_reached`, and `serviced_devices` locals, and the old `slot_starved` handling. Leave the `if failed_devices:` summary, `stale_drops` summary, zero-progress ERROR event, backfill trigger, and result dict exactly as they are.

- [ ] **Step 7: Run the full suite**

Run: `./venv/bin/python -m pytest app -q`
Expected: PASS, with **no change to which runs emit the zero-progress ERROR**. The local `deferred_cleanly` expression above is a like-for-like translation of the old `not retriggered and not budget_starved and not cap_reached` trio: `retrigger_outcome == RETRIGGER_HANDED_OFF` replaces `retriggered`, `RETRIGGER_CAP_REACHED` replaces `cap_reached`, and `traversal.budget_starved` replaces `budget_starved`. A breaker stop still alerts, exactly as before. If a test about breaker-stop alerting fails here, the translation is wrong — fix the expression, do not update the test.

- [ ] **Step 8: Commit**

```bash
git add app/actions/traversal.py app/actions/tests/test_traversal.py app/actions/handlers.py app/actions/tests/
git commit -m "refactor: extract DeviceTraversal and put the head pass on it"
```

---

### Task 7: Put the backfill on the shared traversal and stop it raising

**Files:**
- Modify: `app/actions/handlers.py` (`action_backfill_observations`, ~lines 1279-1400)
- Test: `app/actions/tests/test_backfill.py`

**Interfaces:**
- Consumes: `DeviceTraversal` (Task 6)
- Produces: `action_backfill_observations` gains `result['zero_progress'] = True` on the bad path, matching the shard contract. It no longer raises `LotekException` for zero progress.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_zero_progress_backfill_reports_instead_of_raising(
    mocker, mock_gundi_client_v2, mock_state_manager
):
    """Raising routed through the runner's generic _handle_error, which
    publishes config_data containing the integration's plaintext auth
    (GUNDI-5628). Backfill adopts the head pass's ERROR-event + result-flag
    contract instead (spec D7), and must still suppress the self-retrigger."""
    from app.actions.handlers import action_backfill_observations
    from app.actions.configurations import BackfillObservationsConfig

    mocker.patch("app.actions.handlers._backfill_device", side_effect=ValueError("boom"))
    trigger = mocker.patch("app.actions.handlers.trigger_action", new=AsyncMock())
    try_log = mocker.patch("app.actions.handlers._try_log_activity", new=AsyncMock())

    result = await action_backfill_observations(
        mock_gundi_client_v2.get_integration_details.return_value,
        BackfillObservationsConfig(triggered_by="test"),
    )

    assert result["zero_progress"] is True
    # ERROR activity event carries the health signal...
    from gundi_core.events import LogLevel
    assert try_log.await_args.args[3] is LogLevel.ERROR
    # ...and the cascade stays broken, exactly as the raise used to guarantee.
    trigger.assert_not_awaited()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest app/actions/tests/test_backfill.py::test_zero_progress_backfill_reports_instead_of_raising -v`
Expected: FAIL — `LotekException` is raised instead of returning a result.

- [ ] **Step 3: Write minimal implementation**

In `action_backfill_observations`, replace the loop (from `guards = RunGuards(run_started)` through the end of the `for chunk_start in ...` loop) with:

```python
        guards = RunGuards(run_started)
        traversal = DeviceTraversal(
            integration, "backfill_observations", guards, concurrency=FETCH_CONCURRENCY
        )
        observations_extracted = 0
        gaps_closed = 0
        windows_advanced_total = 0

        async for (device, state), res in traversal.run(
            gapped,
            key=lambda pair: pair[0].nDeviceID,
            process=lambda pair: _backfill_device(
                pair[0], pair[1], integration, auth, pull_config, guards
            ),
        ):
            sent, device_failed, transport_failure, gap_closed, windows_advanced = res
            # Single recording site, mirroring the head pass.
            guards.record(transport_failure=transport_failure)
            observations_extracted += sent
            gaps_closed += int(gap_closed)
            windows_advanced_total += windows_advanced
            if device_failed:
                traversal.mark_failed(device.nDeviceID)

        failed_devices = traversal.failed_devices
        deferred_devices = traversal.deferred_devices

        # Policy: unlike the shard, backfill never re-triggers its own tail from
        # here — the cascade below owns that, throttled by gaps_remaining.
        if traversal.stop_reason:
            await _log_deferral(
                integration, "backfill_observations", traversal.stop_reason,
                deferred_devices,
            )
        if traversal.budget_starved:
            await _log_deferral(
                integration, "backfill_observations", "connection budget exhausted",
                deferred_devices, disposition="to the next backfill trigger",
            )
```

Replace the zero-progress `raise` block with:

```python
        # Backfill's suppression policy differs from the shard's and is
        # preserved exactly: ONLY starvation explains a no-progress run here.
        # Deadline and breaker stops must still alert.
        zero_progress = (
            gapped and traversal.serviced_devices == 0
            and observations_extracted == 0 and not traversal.budget_starved
        )
        if zero_progress:
            # Same systemic-degradation contract as the head pass. Reported, NOT
            # raised: raising routes through the runner's generic _handle_error,
            # which publishes config_data containing every integration
            # configuration — the auth action's plaintext Lotek password
            # included (GUNDI-5628). An ERROR activity event carries the same
            # health signal without the credential exposure (spec D7).
            message = (
                f"No devices could be backfilled for integration {integration_id}: "
                f"{len(failed_devices)} failed, {len(deferred_devices)} deferred of "
                f"{len(gapped)}. See the per-device errors in this action's activity log."
            )
            logger.error(message)
            await _try_log_activity(
                integration_id, "backfill_observations", message, LogLevel.ERROR
            )
```

Add `zero_progress` to the cascade gate — the raise used to break the cascade implicitly, so this must now be explicit:

```python
        breaker_hot = guards.consecutive_transport_failures >= BREAKER_THRESHOLD
        gaps_remaining = (
            any(state.has_gap for _, state in gapped)
            and not breaker_hot
            and windows_advanced_total > 0
            and not traversal.budget_starved
            # A wholly-failing backfill must not re-trigger itself forever. The
            # removed raise used to guarantee this by unwinding; now explicit.
            and not zero_progress
        )
```

And add the flag to the result dict:

```python
        result = {
            'observations_extracted': observations_extracted,
            'devices_failed': failed_devices,
            'devices_deferred': deferred_devices,
            'gaps_closed': gaps_closed,
        }
        if zero_progress:
            # Only present on the bad path, matching the shard contract.
            result['zero_progress'] = True
```

Delete the now-unused `serviced_devices` and `budget_starved` locals and the old `slot_starved` block.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest app/actions/tests/test_backfill.py -v`
Expected: PASS. Any existing test using `pytest.raises(LotekException)` for backfill zero-progress must be rewritten to assert the result flag — that is the intended D7 change. Keep the `LotekException` import only if other tests still use it.

- [ ] **Step 5: Verify the consolidation actually shrank the file**

Run: `wc -l app/actions/handlers.py`
Expected: **≤ 1,240 lines** (from 1,443 — the spec's ≥200-line success criterion). If it is above that, the traversal seam leaked policy; re-read spec D6 and move it back into the handlers rather than adding traversal flags.

- [ ] **Step 6: Commit**

```bash
git add app/actions/handlers.py app/actions/tests/test_backfill.py
git commit -m "refactor: put backfill on the shared traversal and stop it raising"
```

---

### Task 8: Roll back the backfill claim when no command is published

**Files:**
- Modify: `app/actions/handlers.py` (backfill-trigger block, ~lines 788-817)
- Test: `app/actions/tests/test_backfill_trigger_e2e.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (independent; ordered here to keep the refactor commits clean)
- Produces: no new symbols.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_claim_is_released_when_the_trigger_publish_fails(
    mocker, mock_gundi_client_v2, mock_state_manager
):
    """The claim (TTL 540s) is taken before the publish. If the publish fails,
    a stale claim suppresses backfill for every other shard this tick AND the
    next — pre-PR-#20 a lost trigger self-healed on the next run. Roll back."""
    from app.actions.handlers import action_pull_observations_shard, BACKFILL_TRIGGER_CLAIM_SOURCE
    from app.actions.configurations import PullObservationsShardConfig

    mock_state_manager.set_if_absent = AsyncMock(return_value=True)
    mock_state_manager.get_state = AsyncMock(return_value=None)
    mock_state_manager.delete_state = AsyncMock()
    mocker.patch("app.actions.handlers.trigger_action", side_effect=RuntimeError("pubsub down"))
    mocker.patch(
        "app.actions.handlers._head_pass_device",
        return_value=(3, False, False),
    )
    mocker.patch("app.actions.handlers._load_device_state", return_value=(_gapped_state(), False))

    await action_pull_observations_shard(
        mock_gundi_client_v2.get_integration_details.return_value,
        PullObservationsShardConfig(devices=["dev1"]),
    )

    mock_state_manager.delete_state.assert_awaited_once()
    assert mock_state_manager.delete_state.await_args.args[1] == "backfill_observations"
    assert mock_state_manager.delete_state.await_args.kwargs["source_id"] == BACKFILL_TRIGGER_CLAIM_SOURCE
```

Add the helper at module scope in that test file if it is not already present:

```python
def _gapped_state():
    from datetime import datetime, timezone, timedelta
    from app.actions.device_state import DeviceState
    now = datetime.now(tz=timezone.utc)
    return DeviceState(
        high_water=now, gap_start=now - timedelta(days=3), gap_end=now - timedelta(days=1)
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest app/actions/tests/test_backfill_trigger_e2e.py::test_claim_is_released_when_the_trigger_publish_fails -v`
Expected: FAIL — `AssertionError: Expected 'delete_state' to have been awaited once. Awaited 0 times.`

- [ ] **Step 3: Write minimal implementation**

Restructure the backfill-trigger block in `action_pull_observations_shard`:

```python
    if any_open_gap and not zero_progress:
        claimed = False
        published = False
        try:
            # Atomic per-window claim first: concurrent shards all reach this
            # point within seconds of each other, and the lease below is only
            # created a pubsub hop later (when the backfill handler runs), so a
            # plain lease read let every shard publish its own command (review
            # finding). Exactly one shard wins the claim per window.
            claimed = await state_manager.set_if_absent(
                integration_id, "backfill_observations",
                ttl_seconds=app_settings.MAX_ACTION_EXECUTION_TIME,
                source_id=BACKFILL_TRIGGER_CLAIM_SOURCE,
            )
            lease = await state_manager.get_state(
                integration_id, "backfill_observations", BACKFILL_LEASE_SOURCE
            )
            if claimed and not lease:
                # backfill_observations is an InternalActionConfiguration with no
                # persisted portal config, so this MUST carry a non-empty config
                # override — a bare trigger_action publishes an empty
                # config_overrides, which execute_action reads as "no config at
                # all" and 404s before the handler ever runs.
                await trigger_action(
                    integration_id, "backfill_observations",
                    config=BackfillObservationsConfig(
                        triggered_by="pull_observations_shard",
                        # Carry the manual marker: without it a Trigger on a
                        # paused integration pulled head data but its backfill
                        # skipped on the pause, silently importing no history.
                        manual_run=action_config.manual_run,
                    )
                )
                published = True
        except Exception as e:
            # The shard succeeded; a failed trigger must not fail the run.
            logger.warning(
                f"Could not trigger backfill for integration {integration.id}: {describe_exception(e)}"
            )
        finally:
            if claimed and not published:
                # We consumed the per-window claim but never published, so no
                # backfill command exists. Leaving the claim would suppress
                # every other shard this tick AND the next (TTL is a full action
                # budget); pre-sharding a lost trigger self-healed on the next
                # run. Give the claim back (review finding).
                try:
                    await state_manager.delete_state(
                        integration_id, "backfill_observations",
                        source_id=BACKFILL_TRIGGER_CLAIM_SOURCE,
                    )
                except Exception as e:
                    logger.warning(
                        f"Could not release the backfill trigger claim for integration "
                        f"{integration.id} (the TTL will expire it): {describe_exception(e)}"
                    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest app/actions/tests/test_backfill_trigger_e2e.py app/actions/tests/test_sharding.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/actions/handlers.py app/actions/tests/test_backfill_trigger_e2e.py
git commit -m "fix: release the backfill claim when the trigger never publishes"
```

---

### Task 9: Make the dispatcher skip-streak atomic

**Files:**
- Modify: `app/services/state.py` (add `increment_counter`)
- Modify: `app/actions/handlers.py` (`_bump_dispatcher_skip_streak`, ~lines 472-489)
- Test: `app/services/tests/test_state_manager.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `IntegrationStateManager.increment_counter(integration_id, action_id, source_id, ttl_seconds) -> int` — atomic `INCR` plus `EXPIRE`, returning the new value.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_increment_counter_is_atomic_and_expires(state_manager, mock_redis):
    """Client-side get/int+1/set loses increments under concurrency — the same
    reason merge_state_fields exists. INCR is atomic; EXPIRE stops abandoned
    counters leaking keys."""
    mock_redis.incr = AsyncMock(return_value=3)
    mock_redis.expire = AsyncMock(return_value=True)

    value = await state_manager.increment_counter(
        "int-1", "pull_observations", source_id="slot_skip_streak", ttl_seconds=3600
    )

    assert value == 3
    mock_redis.incr.assert_awaited_once()
    mock_redis.expire.assert_awaited_once()
    assert mock_redis.expire.await_args.args[1] == 3600
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest app/services/tests/test_state_manager.py::test_increment_counter_is_atomic_and_expires -v`
Expected: FAIL — `AttributeError: 'IntegrationStateManager' object has no attribute 'increment_counter'`

- [ ] **Step 3: Write minimal implementation**

Add to `IntegrationStateManager` in `app/services/state.py`, following the retry style of its neighbours:

```python
    async def increment_counter(
        self, integration_id: str, action_id: str, source_id: str = "no-source",
        ttl_seconds: int = 3600,
    ) -> int:
        """Atomically increment a small counter and refresh its TTL.

        Used for streak counters. A client-side get / int+1 / set loses
        increments when two runs overlap — the same race merge_state_fields was
        added to close — and an untimed key leaks for anything abandoned
        mid-streak.
        """
        # state.py has no key-builder helper — every method inlines this exact
        # f-string. Match the neighbours rather than introducing one here.
        key = f"integration_state.{integration_id}.{action_id}.{source_id}"
        for attempt in stamina.retry_context(on=redis.RedisError, attempts=5, wait_initial=1.0, wait_max=30, wait_jitter=3.0):
            with attempt:
                value = await self.db_client.incr(key)
                await self.db_client.expire(key, ttl_seconds)
        return int(value)
```

Then replace `_bump_dispatcher_skip_streak` in `app/actions/handlers.py`:

```python
async def _bump_dispatcher_skip_streak(integration_id):
    """Count consecutive dispatcher runs that pulled nothing because the account
    connection budget was saturated. Atomic INCR: two dispatcher runs in the
    same window (schedule tick plus a redelivery or manual trigger) both
    starving would lose an increment under a client-side read-modify-write, and
    the counter would silently never reach DISPATCHER_SKIP_WARN_AFTER."""
    return await state_manager.increment_counter(
        integration_id, "pull_observations",
        source_id=DISPATCHER_SKIP_STREAK_SOURCE,
        ttl_seconds=24 * 3600,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest app/services/tests/test_state_manager.py app/actions/tests/test_sharding.py -v`
Expected: PASS. Tests that stubbed `get_state`/`set_state` for the streak now need `increment_counter` stubbed instead.

- [ ] **Step 5: Commit**

```bash
git add app/services/state.py app/actions/handlers.py app/services/tests/test_state_manager.py app/actions/tests/test_sharding.py
git commit -m "fix: make the dispatcher skip-streak counter atomic and self-expiring"
```

---

### Task 10: Batch the shard's device-state reads

**Files:**
- Modify: `app/actions/handlers.py` (`action_pull_observations_shard` state-loading loop, ~lines 592-597)
- Test: `app/actions/tests/test_sharding.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: no new symbols — reuses the dispatcher's `generate_batches` + `asyncio.gather` pattern and the existing `STATE_READ_CONCURRENCY`.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_shard_loads_device_states_concurrently(mocker, mock_gundi_client_v2, mock_state_manager):
    """The following .sort() forces every state eager, so these reads are a
    serial startup tax with no network I/O to hide behind — paid again on every
    re-trigger hop. The dispatcher's identical pattern was batched in PR #20."""
    from app.actions.handlers import action_pull_observations_shard, STATE_READ_CONCURRENCY
    from app.actions.configurations import PullObservationsShardConfig

    in_flight = 0
    peak = 0

    async def slow_load(*args, **kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return (_plain_state(), False)

    mocker.patch("app.actions.handlers._load_device_state", side_effect=slow_load)
    mocker.patch("app.actions.handlers._head_pass_device", return_value=(0, False, False))

    await action_pull_observations_shard(
        mock_gundi_client_v2.get_integration_details.return_value,
        PullObservationsShardConfig(devices=[f"dev{i}" for i in range(10)]),
    )

    assert peak > 1, "state reads must overlap, not run one-at-a-time"
```

Add the helper if absent:

```python
def _plain_state():
    from datetime import datetime, timezone
    from app.actions.device_state import DeviceState
    return DeviceState(high_water=datetime.now(tz=timezone.utc))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest app/actions/tests/test_sharding.py::test_shard_loads_device_states_concurrently -v`
Expected: FAIL — `AssertionError: state reads must overlap, not run one-at-a-time` (peak is 1)

- [ ] **Step 3: Write minimal implementation**

Replace the sequential loading loop in `action_pull_observations_shard`:

```python
    present_time = datetime.now(tz=timezone.utc)
    # Batched like the dispatcher's fleet-ordering reads: the sort below forces
    # every state eager anyway, so sequential awaits were a flat ~SHARD_SIZE
    # round-trip startup tax per invocation and per re-trigger hop.
    # _load_device_state performs no Redis write (its legacy-migration branch
    # only mutates the in-memory state and returns is_new=True for a later
    # save), so it is side-effect-free and order-independent.
    device_states = []
    for batch in generate_batches(list(action_config.devices), STATE_READ_CONCURRENCY):
        loaded = await asyncio.gather(
            *(
                _load_device_state(integration_id, device_id, present_time, pull_config)
                for device_id in batch
            )
        )
        device_states.extend(
            (device_id, state, is_new)
            for device_id, (state, is_new) in zip(batch, loaded)
        )
    # Least-fresh first within the shard too: a rail cut defers the freshest tail.
    device_states.sort(key=lambda entry: entry[1].high_water)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest app/actions/tests/test_sharding.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/actions/handlers.py app/actions/tests/test_sharding.py
git commit -m "perf: batch the shard's device-state reads like the dispatcher's"
```

---

### Task 11: Verify the whole consolidation

**Files:**
- Modify: none expected (fix-ups only if verification fails)

- [ ] **Step 1: Full suite, with timing**

Run: `./venv/bin/python -m pytest app -q --durations=10`
Expected: PASS, wall time **< 2 s**. If any test now sleeps, patch `SLOT_WAIT_POLL_INITIAL` / `SLOT_WAIT_POLL_MAX` / `SLOT_WAIT_JITTER` to 0 in it.

- [ ] **Step 2: Confirm the test count did not regress**

Run: `./venv/bin/python -m pytest app -q 2>&1 | tail -3`
Expected: **≥ 263 tests** (PR #20's count) — Tasks 1-10 add roughly 15 and rewrite a handful; none should be net deleted except the backfill `pytest.raises(LotekException)` zero-progress cases replaced in Task 7.

- [ ] **Step 3: Confirm the success criteria from the spec**

```bash
wc -l app/actions/handlers.py                    # target: <= 1240 (was 1443)
grep -c "^[A-Z_]* = " app/actions/handlers.py    # constants: expect <= 20 (was 22)
grep -n "raise LotekException" app/actions/handlers.py   # expect: no zero-progress hit
grep -rn "config_data" app/actions/handlers.py   # expect: no matches
```

Expected: all four hold. The last two are the spec's criterion 4 — no `raise`-based leak surface left in the file.

- [ ] **Step 4: Confirm the duplication is actually gone**

```bash
grep -c "return_exceptions=True" app/actions/handlers.py   # expect 0 (moved to traversal.py)
grep -c "LotekUnauthorizedException, asyncio.CancelledError" app/actions/handlers.py  # expect 0
```

Expected: both 0 — the chunked-loop mechanics now exist in exactly one place. If either is non-zero, a handler kept its own copy and Task 6/7 is incomplete.

- [ ] **Step 5: Commit any fix-ups and push**

```bash
git add -A
git commit -m "test: verify rail consolidation end to end"
git push -u origin feat/GUNDI-5626-rail-consolidation
```

- [ ] **Step 6: Stage verification before the large-fleet rollout**

This plan changes concurrency behaviour, and the unit suite cannot prove the queueing arithmetic. Before rolling out to the 616/424/302-device accounts, on stage:

1. Point a stage integration at an account with **more devices than `SHARD_SIZE * (LOTEK_MAX_CONNECTIONS / FETCH_CONCURRENCY)`** — i.e. genuinely oversubscribed. Temporarily lower `SHARD_SIZE` to 5 and `LOTEK_MAX_CONNECTIONS` to 4 to force it on a small account.
2. Confirm in the logs: shards **queue and complete** rather than mass-deferring; zero `cap_reached` ERROR events; `devices_deferred` empty or tiny; every cursor advanced.
3. Confirm exactly **one** backfill invocation across the concurrent shards (the claim still works, and Task 8 did not make it releasable too eagerly).
4. Record the observed queueing time per shard. If it approaches `DEADLINE_FRACTION * MAX_ACTION_EXECUTION_TIME`, raise `LOTEK_MAX_CONNECTIONS` rather than shrinking the partitioning constants (spec D1).
