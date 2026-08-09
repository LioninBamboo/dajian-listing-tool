"""Regression coverage for the rate-limited garden collection client."""

from __future__ import annotations


def test_retry_request_retries_transient_failures_without_sleeping_in_tests():
    from scripts.pilot_collect_garden import retry_request

    calls = []
    sleeps = []

    def flaky_request(value):
        calls.append(value)
        if len(calls) < 3:
            raise RuntimeError("temporary upstream failure")
        return f"ok:{value}"

    result = retry_request(
        flaky_request,
        "sku-batch",
        attempts=3,
        base_delay=12,
        sleep=sleeps.append,
    )

    assert result == "ok:sku-batch"
    assert calls == ["sku-batch", "sku-batch", "sku-batch"]
    assert sleeps == [12, 24]


def test_retry_request_returns_none_after_terminal_failure(capsys):
    from scripts.pilot_collect_garden import retry_request

    def failed_request():
        raise RuntimeError("do not print upstream details")

    result = retry_request(failed_request, attempts=1, sleep=lambda _: None)

    assert result is None
    stderr = capsys.readouterr().err
    assert "failed after 1 attempts" in stderr
    assert "do not print upstream details" not in stderr
