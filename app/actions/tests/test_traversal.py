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
async def test_serviced_devices_discounts_only_caller_marked_failures(integration, mocker):
    # A device the traversal failed never yielded, so it must not be
    # subtracted a second time: serviced_devices counts yielded results minus
    # the ones the caller marked failed. Getting this wrong makes a run that
    # serviced devices quietly look like zero progress, which alerts.
    mocker.patch("app.actions.traversal.log_action_activity", new=AsyncMock())

    async def process(i):
        if i == 1:
            raise ValueError("boom")
        return f"r{i}"

    t = DeviceTraversal(integration, "act", FakeGuards(), concurrency=3)
    async for item, _ in t.run([1, 2, 3], key=str, process=process):
        if item == 2:
            t.mark_failed("2")

    assert t.failed_devices == ["1", "2"]
    assert t.serviced_devices == 1        # device 3 only


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
async def test_backend_unavailable_is_a_failure_not_starvation(integration, mocker):
    """SlotBackendUnavailable means REDIS failed, not that the account is
    saturated (Copilot review, round 5). Classifying it as starvation set
    budget_starved, which suppresses the zero-progress ERROR — so a persistent
    Redis outage looked like a clean capacity deferral and never moved the
    portal health signal. It must take the per-device FAILURE branch instead:
    ERROR-logged, counted failed, budget_starved untouched."""
    from app.services.lotek_connections import SlotBackendUnavailable

    log = mocker.patch("app.actions.traversal.log_action_activity", new=AsyncMock())

    async def process(i):
        if i == 1:
            raise SlotBackendUnavailable("redis down")
        return f"r{i}"

    t = DeviceTraversal(integration, "act", FakeGuards(), concurrency=3)
    seen = [item async for item, _ in t.run([1, 2, 3], key=str, process=process)]

    assert seen == [2, 3]                     # peers unaffected
    assert t.failed_devices == ["1"]          # a FAILURE, visible to zero-progress
    assert t.budget_starved is False          # must NOT suppress the ERROR path
    assert t.slot_starved_devices == []
    assert log.await_count == 1
    assert log.await_args.kwargs["level"] is LogLevel.ERROR


@pytest.mark.asyncio
async def test_wait_budget_exhaustion_defers_as_deadline_not_starvation(integration):
    """Copilot review, round 10: an exhausted computed budget means the run
    crossed its soft deadline mid-chunk — that is DEADLINE policy, not
    capacity starvation. Classifying it as starvation set budget_starved,
    which mislabelled the deferral log and suppressed the backfill's
    zero-progress ERROR (its only clean-deferral case is real saturation).
    The device defers with the guard-stopped cohort — the shard's deadline
    re-trigger picks it up — and budget_starved stays untouched."""
    from app.services.lotek_connections import SlotWaitBudgetExhausted

    async def process(i):
        if i == 1:
            raise SlotWaitBudgetExhausted("soft deadline crossed")
        return f"r{i}"

    t = DeviceTraversal(integration, "act", FakeGuards(), concurrency=3)
    seen = [item async for item, _ in t.run([1, 2, 3], key=str, process=process)]

    assert seen == [2, 3]                     # peers unaffected
    assert t.guard_stopped_devices == ["1"]   # deadline cohort, not starved
    assert t.slot_starved_devices == []
    assert t.budget_starved is False          # must NOT mimic saturation
    assert t.failed_devices == []
    # Copilot round 11: in the LAST (or only) chunk there is no next
    # chunk-boundary should_stop() call, so without recording the reason here
    # stop_reason stayed None — the shard neither logged nor re-triggered the
    # deferral and could emit a spurious zero-progress ERROR. Exhaustion IS a
    # deadline detection on the same clock, so it records the same reason.
    assert t.stop_reason == "deadline"


@pytest.mark.asyncio
async def test_mark_slot_starved_registers_caller_detected_starvation(integration):
    """Copilot round 11: a backfill device that advances a window and then
    starves RETURNS a result (round 10), so the traversal never sees the
    exception — the device vanished from devices_deferred and no
    connection-budget WARNING said why the cascade throttled. The caller
    reports it through the same bookkeeping the exception path uses,
    mirroring mark_failed."""
    t = DeviceTraversal(integration, "act", FakeGuards(), concurrency=2)
    _ = [item async for item, _ in t.run([1], key=str, process=_ok)]

    t.mark_slot_starved("1")

    assert t.slot_starved_devices == ["1"]
    assert t.budget_starved is True
    assert t.deferred_devices == ["1"]
    assert t.failed_devices == []


@pytest.mark.asyncio
async def test_mark_deadline_cut_registers_caller_detected_deadline(integration):
    """Copilot round 12: the deadline twin of mark_slot_starved. A backfill
    device that advances a window and then hits the soft deadline on a later
    acquire RETURNS partial progress — in a last/only chunk no boundary check
    follows, so without caller registration the device vanished from
    devices_deferred, no deadline deferral was logged, and stop_reason stayed
    unset while its gap was unfinished."""
    t = DeviceTraversal(integration, "act", FakeGuards(), concurrency=2)
    _ = [item async for item, _ in t.run([1], key=str, process=_ok)]

    t.mark_deadline_cut("1")

    assert t.guard_stopped_devices == ["1"]
    assert t.stop_reason == "deadline"
    assert t.budget_starved is False          # deadline, not saturation
    assert t.deferred_devices == ["1"]


@pytest.mark.asyncio
async def test_backend_unavailable_stops_the_traversal_after_the_chunk(integration, mocker):
    """Copilot round 13: SlotBackendUnavailable is an ACCOUNT-WIDE backend
    failure, but it was handled as an isolated device failure with
    transport_failure=False — the breaker never opened, so a persistent
    Redis outage made every remaining device grind through its full bounded
    acquire and publish an ERROR each (potentially hundreds across the
    fan-out). A shared-backend failure now stops the traversal after the
    current chunk: peers in the chunk keep their results, the untouched
    tail defers under stop_reason "redis unavailable", and budget_starved
    stays clear (this is not saturation)."""
    from app.services.lotek_connections import SlotBackendUnavailable

    log = mocker.patch("app.actions.traversal.log_action_activity", new=AsyncMock())

    async def process(i):
        if i == 2:
            raise SlotBackendUnavailable("redis down")
        return f"r{i}"

    t = DeviceTraversal(integration, "act", FakeGuards(), concurrency=2)
    seen = [item async for item, _ in t.run([1, 2, 3, 4, 5], key=str, process=process)]

    assert seen == [1]                          # chunk peer kept its result
    assert t.failed_devices == ["2"]            # the device that hit it: per-device ERROR
    assert log.await_count == 1
    assert t.guard_stopped_devices == ["3", "4", "5"]  # untouched tail deferred
    assert t.stop_reason == "redis unavailable"
    assert t.backend_unavailable is True
    assert t.budget_starved is False


@pytest.mark.asyncio
async def test_mark_backend_cut_registers_partial_progress_backend_failure(integration):
    """The backend twin of mark_deadline_cut: a backfill device that advanced
    a window and then hit a Redis failure RETURNS partial progress, so the
    traversal never sees the exception — register the remaining work as
    deferred and record the backend stop."""
    t = DeviceTraversal(integration, "act", FakeGuards(), concurrency=2)
    _ = [item async for item, _ in t.run([1], key=str, process=_ok)]

    t.mark_backend_cut("1")

    assert t.guard_stopped_devices == ["1"]
    assert t.stop_reason == "redis unavailable"
    assert t.backend_unavailable is True
    assert t.budget_starved is False


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


@pytest.mark.asyncio
async def test_slot_starvation_and_guard_stop_stay_in_separate_lists(integration):
    # A chunk that starves on slots can itself burn enough wall-clock time
    # (queueing for a peer to release) that the deadline guard trips on the
    # very next chunk. Both used to land in one shared deferred_devices list,
    # so a caller retriggering/logging "deadline" devices picked up the
    # starved one too, and the budget-starved log picked up the untouched
    # tail — each device reported under the wrong reason, and reported twice
    # overall (review finding). They must stay in disjoint, reason-specific
    # lists, with `deferred_devices` only their union.
    async def process(i):
        if i == 1:
            raise NoConnectionSlot("saturated")
        return f"r{i}"

    t = DeviceTraversal(integration, "act", FakeGuards(stop_after=1), concurrency=2)
    seen = [item async for item, _ in t.run([1, 2, 3, 4], key=str, process=process)]

    assert seen == [2]                               # device 1 starved, chunk 1 processed
    assert t.slot_starved_devices == ["1"]
    assert t.guard_stopped_devices == ["3", "4"]      # unreached tail only
    assert sorted(t.deferred_devices) == ["1", "3", "4"]  # union
    assert t.budget_starved is True
    assert t.stop_reason == "deadline"
