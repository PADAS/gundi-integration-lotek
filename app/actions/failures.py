"""Why a device failed in a run, and the plain-language portal messages for it.

Messages say only what happened in that run. They never predict later runs
("will be retried"): a log can't know that, and old logs are never edited.
"""
from datetime import timedelta
from enum import Enum


class FailureReason(str, Enum):
    TIMEOUT = "timeout"
    NO_CONNECTION = "no_connection"
    LOTEK_ERROR = "lotek_error"
    LOGIN_REJECTED = "login_rejected"
    DELIVERY_FAILED = "delivery_failed"
    SAVE_FAILED = "save_failed"
    UNEXPECTED = "unexpected"


# Temporary reasons are WARNINGs until the same device has kept failing for
# ESCALATION_AFTER; the rest are errors right away (they get their own
# per-device ERROR line where they happen).
TEMPORARY_REASONS = frozenset({
    FailureReason.TIMEOUT,
    FailureReason.NO_CONNECTION,
    FailureReason.LOTEK_ERROR,
    FailureReason.LOGIN_REJECTED,
})
ESCALATION_AFTER = timedelta(hours=1)

_WARNING_MESSAGES = {
    FailureReason.TIMEOUT: "Lotek didn't send data for {count} because the connection timed out.",
    FailureReason.NO_CONNECTION: "Lotek didn't send data for {count} because Gundi couldn't connect to it.",
    FailureReason.LOTEK_ERROR: "Lotek didn't send data for {count} because its server returned an error.",
    FailureReason.LOGIN_REJECTED: "Lotek didn't send data for {count} because it rejected Gundi's login.",
    FailureReason.DELIVERY_FAILED: "Lotek sent data for {count}, but Gundi couldn't receive it.",
    FailureReason.SAVE_FAILED: "Gundi got the latest data for {count} but couldn't save how far it got.",
    FailureReason.UNEXPECTED: "Something unexpected went wrong getting data for {count}. The error for each device is in this log.",
}

_ESCALATED_MESSAGES = {
    FailureReason.TIMEOUT: "Lotek hasn't sent data for {count} for over 1 hour because the connection keeps timing out.",
    FailureReason.NO_CONNECTION: "Lotek hasn't sent data for {count} for over 1 hour because Gundi keeps failing to connect to it.",
    FailureReason.LOTEK_ERROR: "Lotek hasn't sent data for {count} for over 1 hour because its server keeps returning errors.",
    FailureReason.LOGIN_REJECTED: "Lotek hasn't sent data for {count} for over 1 hour because it keeps rejecting Gundi's login.",
}

# Short "because ..." endings for the one-line zero-progress error.
_LABELS = {
    FailureReason.TIMEOUT: "the connection timed out",
    FailureReason.NO_CONNECTION: "Gundi couldn't connect to Lotek",
    FailureReason.LOTEK_ERROR: "Lotek's server returned an error",
    FailureReason.LOGIN_REJECTED: "Lotek rejected Gundi's login",
    FailureReason.DELIVERY_FAILED: "Gundi couldn't receive the data",
    FailureReason.SAVE_FAILED: "Gundi couldn't save how far it got",
    FailureReason.UNEXPECTED: "something unexpected went wrong",
}


def as_reason(reason):
    """Any value to a FailureReason; unknown values become UNEXPECTED so a
    summary can never break after the run's work is done."""
    try:
        return FailureReason(reason)
    except ValueError:
        return FailureReason.UNEXPECTED


def device_count(ids_text, failed, total):
    noun = "device" if total == 1 else "devices"
    return f"{failed} of {total} {noun} ({ids_text})"


def warning_message(reason, count):
    return _WARNING_MESSAGES[as_reason(reason)].format(count=count)


def escalated_message(reason, count):
    reason = as_reason(reason)
    template = _ESCALATED_MESSAGES.get(reason, _WARNING_MESSAGES[reason])
    return template.format(count=count)


def nothing_updated_message(total, failed_by_reason, deferred, done="updated", group=""):
    """One line for a run where no device got through, e.g. "None of the 25
    devices could be updated in this run: 20 because the connection timed
    out, 5 because Lotek rejected Gundi's login." """
    if total == 1:
        reasons = [_LABELS[as_reason(reason)] for reason in failed_by_reason]
        detail = f" because {' and '.join(reasons)}" if reasons else ""
        if deferred:
            detail = " because it was postponed to a later run"
        subject = f"The device {group}" if group else "The device"
        return f"{subject} couldn't be {done} in this run{detail}."
    parts = [
        f"{len(ids)} because {_LABELS[as_reason(reason)]}"
        for reason, ids in failed_by_reason.items()
    ]
    if deferred:
        parts.append(f"{deferred} postponed to a later run")
    detail = f": {', '.join(parts)}" if parts else ""
    subject = f"None of the {total} devices {group}" if group else f"None of the {total} devices"
    return f"{subject} could be {done} in this run{detail}."
