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
