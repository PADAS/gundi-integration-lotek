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

    def __init__(self, integration, action_id, guards, *, concurrency=1):
        self.integration = integration
        self.integration_id = str(integration.id)
        self.action_id = action_id
        self.guards = guards
        # Work partitioning, not a concurrency limit — the account-wide ceiling
        # lives in the Redis slot (spec D1). Callers pass their chunk width; the
        # default of 1 just means "one device per chunk".
        self.concurrency = concurrency
        self.failed_devices = []
        # Two reason-specific lists, not one combined `deferred_devices`:
        # guard_stopped_devices is the unreached tail cut short by
        # should_stop() (deadline/breaker/cap); slot_starved_devices is
        # per-device NoConnectionSlot within an attempted chunk. Callers used
        # to share one list for both, so a chunk that starved on slots right
        # before a deadline cut got its starved devices re-triggered/logged as
        # the deadline tail, and the untouched tail got mislabeled as slot
        # starvation — devices reported under the wrong reason, and reported
        # twice when both conditions applied to the same run (review finding).
        self.guard_stopped_devices = []
        self.slot_starved_devices = []
        self.stop_reason: Optional[str] = None
        self.budget_starved = False
        self._yielded = 0
        self._marked_failed = 0

    @property
    def deferred_devices(self):
        """Union of every deferred device, for the overall reported count and
        result only. Stop-reason and budget-starved handling must use
        guard_stopped_devices / slot_starved_devices directly instead of this
        union — see the constructor comment."""
        return self.guard_stopped_devices + self.slot_starved_devices

    @property
    def serviced_devices(self):
        # Only the caller knows whether a yielded result counts as success, so
        # it calls mark_failed() for the ones that don't. Discount ONLY those:
        # a device whose exception the traversal logged never yielded, so
        # subtracting all of failed_devices would count it twice and could make
        # a run that serviced devices quietly (nothing new to send) look like
        # zero progress — which alerts.
        return self._yielded - self._marked_failed

    def mark_failed(self, device_id):
        """Caller-side failure: the device produced a result, but the result
        says it failed (e.g. delivery rejected)."""
        self.failed_devices.append(device_id)
        self._marked_failed += 1

    async def run(self, work, key, process):
        work = list(work)
        for chunk_start in range(0, len(work), self.concurrency):
            if reason := self.guards.should_stop():
                self.stop_reason = reason
                self.guard_stopped_devices.extend(key(item) for item in work[chunk_start:])
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
                    self.slot_starved_devices.append(device_id)
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
