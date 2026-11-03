from app.actions.failures import (
    FailureReason, as_reason, device_count, escalated_message, nothing_updated_message, warning_message,
)


def test_unknown_reason_falls_back_to_unexpected():
    assert as_reason("no_such_reason") is FailureReason.UNEXPECTED
    assert warning_message("no_such_reason", "1 of 1 device (1)").startswith("Something unexpected")


def test_device_count_reads_naturally():
    assert device_count("88630", 1, 1) == "1 of 1 device (88630)"
    assert device_count("1, 2", 2, 25) == "2 of 25 devices (1, 2)"


def test_escalated_message_for_a_temporary_reason():
    assert escalated_message(FailureReason.LOGIN_REJECTED, "1 of 2 devices (1)") == (
        "Lotek hasn't sent data for 1 of 2 devices (1) for over 1 hour because it keeps rejecting Gundi's login."
    )


def test_escalated_message_falls_back_to_the_warning_text():
    assert escalated_message(FailureReason.DELIVERY_FAILED, "x") == warning_message(FailureReason.DELIVERY_FAILED, "x")


def test_nothing_updated_names_each_reason_and_the_postponed():
    message = nothing_updated_message(
        25, {FailureReason.TIMEOUT: list(range(20)), FailureReason.LOGIN_REJECTED: list(range(3))}, 2,
    )
    assert message == (
        "None of the 25 devices could be updated in this run: 20 because the connection timed out, "
        "3 because Lotek rejected Gundi's login, 2 postponed to a later run."
    )


def test_nothing_updated_for_a_single_device():
    assert nothing_updated_message(1, {FailureReason.TIMEOUT: ["1"]}, 0, done="backfilled") == (
        "The device couldn't be backfilled in this run because the connection timed out."
    )
