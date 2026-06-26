"""Exponential backoff with pause-after-N-consecutive-failures.

Ported literally from the long-running system's regression-backoff
state machine: every consecutive failure doubles the sleep, capped at
``max_sec``; after ``pause_after_n`` consecutive failures, sleep jumps
to ``pause_sec`` so an operator can inspect instead of the supervisor
producing more empty cycles.
"""

from __future__ import annotations


class Backoff:
    """State machine for cycle-level backoff.

    Args:
        initial_sec: Sleep duration on a clean cycle, and the base for
            the exponential schedule on failures.
        max_sec: Cap for the doubling schedule. After enough failures,
            sleeps are clamped to this value (and then to ``pause_sec``
            once ``pause_after_n`` is crossed).
        pause_after_n: Consecutive-failure count at which sleep jumps
            to ``pause_sec`` instead of continuing to double. Set to a
            very large number to disable the long pause.
        pause_sec: The long-pause duration. Default 24h matches the
            original system's "operator should look at this" cadence.
    """

    def __init__(
        self,
        initial_sec: float = 60.0,
        max_sec: float = 3600.0,
        pause_after_n: int = 3,
        pause_sec: float = 24 * 3600,
    ) -> None:
        if initial_sec <= 0:
            raise ValueError("initial_sec must be positive")
        if max_sec < initial_sec:
            raise ValueError("max_sec must be >= initial_sec")
        if pause_after_n < 1:
            raise ValueError("pause_after_n must be >= 1")
        self.initial = float(initial_sec)
        self.max = float(max_sec)
        self.pause_after_n = pause_after_n
        self.pause = float(pause_sec)
        self._consecutive_failures = 0

    def failure(self) -> float:
        """Record a failed cycle. Returns the next sleep duration."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.pause_after_n:
            return self.pause
        # Exponential: initial * 2^(n-1)
        return min(self.initial * (2 ** (self._consecutive_failures - 1)), self.max)

    def success(self) -> float:
        """Record a clean cycle. Resets the failure counter."""
        self._consecutive_failures = 0
        return self.initial

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def paused(self) -> bool:
        """True iff the next failure() call would return the long-pause
        duration (i.e. we are at or above ``pause_after_n``)."""
        return self._consecutive_failures >= self.pause_after_n

    def reset(self) -> None:
        self._consecutive_failures = 0
