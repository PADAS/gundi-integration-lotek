# Lotek connector: consolidate the accumulated safety rails

**Ticket**: GUNDI-5626 (proposed) · **Status**: draft 2026-08-20 · **Repo**: gundi-integration-lotek

## Problem

Seven PRs (#14–#20) landed on `app/actions/handlers.py` between 2026-08-12 and 2026-08-17,
each a locally correct response to a real production symptom. The file went from **354 lines
on `main` to 1,443 lines at PR #20's head — 4.1x**. It now carries 22 module-level constants
and eight distinct coordination mechanisms (backfill lease, backfill trigger claim, dispatcher
skip streak, Redis connection slot, re-trigger generation counter, in-memory breaker, deadline
fraction, per-device gap fields).

Nothing in that history was gold-plating. The problem is that each fix **added a governor
without reconciling it against the existing ones**, so the intermediate scaffolding is all
still present and the mechanisms now interact in ways no single-mechanism review catches.

Two concrete symptoms:

1. **Un-reconciled concurrency arithmetic.** The PR #20 review's top finding is not a bug
   inside any one mechanism — it is a bug in their composition. A 616-device account fans
   out 25 shards, each opening a `FETCH_CONCURRENCY = 5` chunk, so ~125 slot acquires race a
   `LOTEK_MAX_CONNECTIONS = 20` ceiling. `lotek_slot` is fail-fast, and a *single* refused
   device defers that shard's **entire remaining tail** and re-triggers with no backoff. On
   exactly the fleets sharding was built to serve, zero-progress churn and cap-reached portal
   ERRORs become steady state.

2. **A ~270-line traversal, twice.** `action_pull_observations_shard` (273 lines) and
   `action_backfill_observations` (272 lines) implement the same chunked device loop —
   `RunGuards`, `should_stop`, `asyncio.gather(..., return_exceptions=True)`, fatal re-raise,
   `slot_starved` collection, per-device failure logging, deferred-tail computation. The two
   copies have already drifted once (`cap_reached` is tracked in one and not the other;
   harmless today only because a different boolean happens to suppress the same branch).

## Design

### D1. Exactly one concurrency governor

The three constants that look like concurrency knobs have distinct jobs, and conflating them
is the root of symptom 1. After this change:

| Constant | Role | Is it a concurrency limit? |
|---|---|---|
| `LOTEK_MAX_CONNECTIONS` (20) | Account-wide ceiling on simultaneous Lotek requests, enforced in Redis across invocations | **Yes — the only one** |
| `FETCH_CONCURRENCY` (5) | How many devices one invocation processes per chunk | No — in-process batching |
| `SHARD_SIZE` (25) | How much work fits in one action budget | No — work partitioning |

`FETCH_CONCURRENCY` and `SHARD_SIZE` become *work-partitioning* parameters that may
oversubscribe the budget freely, because the budget itself now applies backpressure instead
of refusing. This is documented at the constants and pinned by a test.

### D2. The slot waits instead of refusing

`lotek_slot` becomes a **bounded, deadline-aware wait**: poll the acquire with jittered
backoff until a slot frees, giving up only when waiting longer would eat the caller's own
action deadline. `NoConnectionSlot` is then a genuine "the account is saturated for longer
than my remaining budget" signal rather than "someone else got there first this millisecond".

Rationale: with 20 slots and per-request latencies of ~1–3s, a 616-device fleet's ~616
requests drain in ~30–90s of queueing, comfortably inside a 540s budget. Waiting converts
starvation from a whole-tail abort into a short queue. The deadline guard still protects the
budget, so the change cannot cause a timeout that fail-fast would have avoided.

Rejected alternative: have the dispatcher fan out in waves sized to the budget. The dispatcher
publishes fire-and-forget pubsub messages and never learns when a shard finishes, so it cannot
sequence waves. Backpressure has to live at the resource.

### D3. Starvation defers only the starved devices

Even with D2, a genuine saturation must not abort a whole shard. The deferral narrows to the
devices that actually failed to get a slot; their in-chunk peers keep their results and the
loop continues.

### D4. The slot acquire becomes idempotent

The acquire token is generated once *outside* the retry loop and the Lua gates purely on
`ZCARD`, so a lost reply on the acquire that takes the last slot refuses the very caller that
owns it and leaks that slot for the full 300s TTL. A `ZSCORE` membership fast-path returning 1
for an already-present token makes the retry idempotent. This is a **prerequisite** for D2:
waiting means more acquire attempts, which makes the lost-reply window materially more likely.

### D5. The slot uses the codebase's Redis retry policy

`lotek_connections.py` re-implements the retry policy as `attempts=3, wait_initial=0.1,
wait_max=2.0` under a comment claiming parity with `IntegrationStateManager`, whose fourteen
Redis call sites all use `attempts=5, wait_initial=1.0, wait_max=30, wait_jitter=3.0`. A
multi-second Redis brownout that every `get_state` survives exhausts the slot's sub-second
budget and escapes as `RedisError` into the generic per-device handler — a **fabricated Lotek
failure caused by our own Redis**, which is exactly what the comment claims to prevent. Adopt
the shared policy.

### D6. One device traversal

Extract the shared mechanics into a `DeviceTraversal` helper that owns: chunking, the
`gather` with `return_exceptions=True`, fatal re-raise of `LotekUnauthorizedException` /
`CancelledError`, per-device failure logging, slot-starvation collection, guard-stop
detection, and deferred-tail computation.

The seam is deliberate: **mechanics in the traversal, policy in the caller.** The traversal
does not decide whether to re-trigger, what the deferral message says, or what zero progress
means — those differ between head pass and backfill and stay in the handlers. Callers fold
their own accumulators inline via `async for`, so `gaps_closed` / `windows_advanced_total` /
`any_open_gap` need no callback plumbing.

This also retires the three parallel booleans (`retriggered` / `budget_starved` /
`cap_reached`) in favour of traversal attributes plus one locally-computed
`deferred_cleanly` expression per caller, which is what drifted in symptom 2.

The suppression expression stays in the **caller**, not the traversal: the two handlers
genuinely disagree about what counts as a clean deferral. The shard suppresses its
zero-progress ERROR on a successful hand-off, a cap-reached deferral, or starvation, but
*not* on a breaker stop; the backfill suppresses on starvation only. Hoisting a single
`deferred_cleanly` onto the traversal would silently suppress outage alerts in both — and
the cdip health metric counts ERROR events, so that would make a real Lotek outage look
healthier than it is. Alerting behaviour must be unchanged by this refactor.

### D7. Backfill zero-progress stops raising

`action_backfill_observations` still `raise`s `LotekException` on zero progress. That routes
through the runner's generic `_handle_error`, which publishes `config_data` containing every
integration configuration — the auth action's plaintext Lotek password included (see
**GUNDI-5628**). The head pass already replaced this with an ERROR activity event plus a
machine-readable result flag; backfill adopts the same form. This removes the last credential-
leak-by-`raise` in the file and makes the two handlers' zero-progress contract identical,
which is a precondition for them sharing the traversal cleanly.

Behaviour change to note: the raise previously also broke the self-retrigger cascade. The
replacement must keep that property explicitly — `zero_progress` suppresses `gaps_remaining`.

### D8. Small fixes folded in

- **Backfill claim rollback.** The `set_if_absent` claim (TTL 540s) is taken *before* the
  lease check and the publish, and is never rolled back, so a swallowed publish failure
  suppresses backfill for every other shard this tick and the next. Delete the claim when the
  publish does not happen.
- **Skip-streak counter.** Client-side get/increment/set with no TTL, in the same file whose
  state manager added atomic Lua merge because client-side read-modify-write loses updates.
  Move to `INCR` + `EXPIRE`.
- **Shard state batching.** The shard loads its 25 device states with sequential awaits, and
  the following `.sort()` forces them all eager, while the dispatcher's identical pattern was
  converted to bounded-concurrency `gather` in PR #20. Reuse that pattern.

## Non-goals

- **Per-shard portal reload.** `execute_action` always resolves the action config, and the
  internal `pull_observations_shard` key is never cached, so every shard forces a full Gundi
  portal round trip (~25/tick on a large account). The fix belongs in `config_manager`
  (negative caching for internal actions), which is vendored template code — tracked with
  **GUNDI-5628** rather than diverging this repo's copy.
- **`TRIGGER_ACTIONS_ALWAYS_SYNC` nested budgets.** Real, but the flag is dev-only, defaults
  false, and appears in no env file, compose file, or CI workflow.
- **Dispatcher demand shaping on healthy ticks.** D2 makes it unnecessary; revisit only if
  the 616-device rollout shows queueing dominating the budget.
- **Behaviour changes to what gets fetched or delivered.** This is a structural consolidation.
  Observation output must be byte-identical.

## Success criteria

**Amended 2026-08-20, mid-execution.** Criterion 1 originally read "`handlers.py` drops by
≥ 200 lines". That was a bad proxy and is corrected here rather than quietly re-banded.
Extracting shared mechanics into a new module **relocates** lines and adds a class scaffold,
docstrings, and imports; it does not delete 200 of them. This plan also deliberately *adds*
code (D1's comments, the per-caller suppression expressions, D8's claim-rollback `finally`).
Measured after the head-pass conversion: `handlers.py` 1443 → 1398, with a new 107-line
`traversal.py`, so combined lines went *up* by 62. The consolidation's value is that the
loop logic exists **once** instead of twice — which shows up in the duplication greps and in
the per-handler length, not in a whole-file total. Do not cut behaviour or move policy back
into the traversal to chase a line count.

1. The two duplication greps over `handlers.py` both return **0**: `return_exceptions=True`
   and `LotekUnauthorizedException, asyncio.CancelledError`. This is the binding criterion —
   it is what proves the chunked-loop mechanics exist in exactly one place.
2. Each of the two loop-bearing handlers drops by roughly **45-55 lines**:
   `action_pull_observations_shard` 273 → ~225 (measured: 225), and
   `action_backfill_observations` 274 → ~226.
3. `handlers.py` lands near **1,350** lines (a ~6% reduction), and `handlers.py` +
   `traversal.py` combined stays roughly flat against the 1,443 baseline.
4. A test pins that oversubscribed fan-out (shards × `FETCH_CONCURRENCY` > `LOTEK_MAX_CONNECTIONS`)
   makes progress rather than mass-deferring.
5. A test pins that a lost acquire reply does not strand a slot.
6. No `raise` in either action's zero-progress path (`grep -n "raise LotekException"` over
   `handlers.py` returns nothing), and therefore no route from a zero-progress run into the
   runner's `_handle_error` `config_data` publish. Note the literal `config_data` string
   still appears in `handlers.py` inside explanatory comments that name the leak being
   avoided — the criterion is about the code path, not the token.
7. Full suite green, under 3.0s, with no net loss of test count.
8. **Alerting is unchanged.** Which runs emit a zero-progress ERROR must be identical
   before and after: the shard suppresses on hand-off / cap-reached / starvation but not on
   a breaker stop; the backfill suppresses on starvation only.
