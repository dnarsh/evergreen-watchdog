"""Backoff state-machine tests — pure logic, no I/O."""

from __future__ import annotations

import pytest

from evergreen_watchdog import Backoff


def test_clean_cycle_sleeps_initial() -> None:
    b = Backoff(initial_sec=60, max_sec=3600, pause_after_n=3)
    assert b.success() == 60.0
    assert b.consecutive_failures == 0
    assert not b.paused


def test_failures_double_until_cap() -> None:
    b = Backoff(initial_sec=60, max_sec=300, pause_after_n=99)
    assert b.failure() == 60  # 60 * 2^0
    assert b.failure() == 120  # 60 * 2^1
    assert b.failure() == 240  # 60 * 2^2
    assert b.failure() == 300  # capped at max_sec, not 480


def test_pause_after_n_jumps_to_pause_sec() -> None:
    b = Backoff(initial_sec=60, max_sec=3600, pause_after_n=3, pause_sec=86400)
    assert b.failure() == 60
    assert b.failure() == 120
    # 3rd failure triggers the long pause, not exponential 240
    assert b.failure() == 86400
    assert b.paused
    # Further failures stay at pause_sec
    assert b.failure() == 86400


def test_success_resets_failure_counter() -> None:
    b = Backoff(initial_sec=60, pause_after_n=3)
    b.failure()
    b.failure()
    assert b.consecutive_failures == 2
    b.success()
    assert b.consecutive_failures == 0
    assert not b.paused
    # Schedule resumes from scratch
    assert b.failure() == 60


def test_rejects_bad_args() -> None:
    with pytest.raises(ValueError):
        Backoff(initial_sec=0)
    with pytest.raises(ValueError):
        Backoff(initial_sec=60, max_sec=30)
    with pytest.raises(ValueError):
        Backoff(pause_after_n=0)
