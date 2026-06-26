"""Watchdog supervisor tests.

These run the real Watchdog class against real targets (callables) with
short backoffs and max_cycles set, so they finish in seconds rather than
hours. The singleton-lock test spawns a real subprocess to verify the
cross-process behavior.
"""

from __future__ import annotations

import multiprocessing as mp
import sys
import time
from pathlib import Path

import pytest

from evergreen_watchdog import Backoff, FileFlagGate, Watchdog


def _fast_backoff() -> Backoff:
    return Backoff(initial_sec=0.05, max_sec=0.2, pause_after_n=2, pause_sec=0.5)


def test_successful_cycles_run_to_max_then_exit(tmp_path: Path) -> None:
    calls = []

    def target() -> int:
        calls.append(time.monotonic())
        return 0

    wd = Watchdog(
        target=target,
        lock_path=tmp_path / "wd.pid",
        backoff=_fast_backoff(),
        max_cycles=3,
    )
    rc = wd.run()
    assert rc == 0
    assert len(calls) == 3
    assert wd.cycle_count == 3
    # Lock file should be released on exit
    assert not (tmp_path / "wd.pid").exists()


def test_failing_target_triggers_backoff_then_pause(tmp_path: Path) -> None:
    calls = []

    def target() -> int:
        calls.append(time.monotonic())
        return 1

    backoff = Backoff(initial_sec=0.05, max_sec=0.05, pause_after_n=2, pause_sec=0.3)
    wd = Watchdog(
        target=target,
        lock_path=tmp_path / "wd.pid",
        backoff=backoff,
        max_cycles=3,
    )

    started = time.monotonic()
    wd.run()
    elapsed = time.monotonic() - started

    assert len(calls) == 3
    # After cycle 2 we hit pause_after_n=2 → sleep should be ~0.3s.
    # After cycle 3 same. So total elapsed >= ~0.6s. Allow generous slack.
    assert elapsed >= 0.5, f"backoff didn't escalate; elapsed={elapsed:.2f}s"


def test_exception_in_target_counts_as_failure(tmp_path: Path) -> None:
    calls = []

    def target() -> int:
        calls.append(1)
        raise RuntimeError("boom")

    wd = Watchdog(
        target=target,
        lock_path=tmp_path / "wd.pid",
        backoff=_fast_backoff(),
        max_cycles=2,
    )
    wd.run()
    assert len(calls) == 2
    # The Watchdog should have counted both as failures
    assert wd.backoff.consecutive_failures >= 2


def test_blocking_gate_skips_cycle_then_runs_when_unblocked(tmp_path: Path) -> None:
    """Gate blocks for the first few checks, then opens; the watchdog
    must skip the blocked cycles and run when the gate opens."""
    flag = tmp_path / "pause.flag"
    flag.write_text("")  # initially blocking

    calls = []

    def target() -> int:
        calls.append(1)
        return 0

    backoff = Backoff(initial_sec=0.05, max_sec=0.05, pause_after_n=99)
    wd = Watchdog(
        target=target,
        lock_path=tmp_path / "wd.pid",
        gates=[FileFlagGate(flag)],
        backoff=backoff,
        check_interval=0.05,
        max_cycles=1,
    )

    # Open the gate from a background thread after a short delay
    import threading

    def _open_gate_soon():
        time.sleep(0.2)
        flag.unlink()

    threading.Thread(target=_open_gate_soon, daemon=True).start()

    started = time.monotonic()
    wd.run()
    elapsed = time.monotonic() - started

    assert len(calls) == 1, f"expected exactly 1 target invocation, got {len(calls)}"
    assert elapsed >= 0.15, "should have waited for gate to open"


def test_stop_request_exits_cleanly_mid_sleep(tmp_path: Path) -> None:
    """stop() called between cycles should exit within the sleep slice
    granularity, not wait for the full backoff."""
    calls = []

    def target() -> int:
        calls.append(1)
        return 0

    backoff = Backoff(initial_sec=5.0, max_sec=5.0, pause_after_n=99)
    wd = Watchdog(
        target=target,
        lock_path=tmp_path / "wd.pid",
        backoff=backoff,
        max_cycles=10,
    )

    import threading

    def _stop_soon():
        time.sleep(0.3)
        wd.stop()

    threading.Thread(target=_stop_soon, daemon=True).start()

    started = time.monotonic()
    wd.run()
    elapsed = time.monotonic() - started

    assert len(calls) >= 1
    # Should NOT have waited the full 5s backoff
    assert elapsed < 3.0, f"stop didn't take effect; elapsed={elapsed:.2f}s"


def _singleton_child(lock_path: str, result_q: "mp.Queue[int]") -> None:
    """Run a watchdog that holds the lock for a measurable time."""
    from evergreen_watchdog import Backoff, Watchdog

    def slow_target() -> int:
        time.sleep(2.0)
        return 0

    wd = Watchdog(
        target=slow_target,
        lock_path=lock_path,
        backoff=Backoff(initial_sec=0.05, max_sec=0.05, pause_after_n=99),
        max_cycles=1,
    )
    rc = wd.run()
    result_q.put(rc)


def test_singleton_lock_prevents_second_supervisor(tmp_path: Path) -> None:
    """Two supervisors with the same lock path — exactly one runs, the
    other exits with code 75 (EX_TEMPFAIL)."""
    lock = str(tmp_path / "wd.pid")
    ctx = mp.get_context("spawn")
    result_q: "mp.Queue[int]" = ctx.Queue()

    # Start the holder
    holder = ctx.Process(target=_singleton_child, args=(lock, result_q))
    holder.start()
    # Give the holder time to acquire
    time.sleep(0.5)

    # Try to start a second supervisor inline — should fail to acquire
    def target() -> int:
        return 0

    intruder = Watchdog(
        target=target,
        lock_path=lock,
        backoff=Backoff(initial_sec=0.05, max_sec=0.05, pause_after_n=99),
        max_cycles=10,
    )
    rc = intruder.run()
    assert rc == 75, f"expected EX_TEMPFAIL (75), got {rc}"

    holder.join(timeout=10)
    assert not holder.is_alive()
    holder_rc = result_q.get(timeout=2)
    assert holder_rc == 0


def test_subprocess_target_runs_and_exit_code_propagates(tmp_path: Path) -> None:
    """Use real subprocess target — Python itself, with a tiny script."""
    wd = Watchdog(
        target=[sys.executable, "-c", "import sys; sys.exit(0)"],
        lock_path=tmp_path / "wd.pid",
        backoff=_fast_backoff(),
        max_cycles=2,
    )
    rc = wd.run()
    assert rc == 0
    assert wd.backoff.consecutive_failures == 0


def test_subprocess_target_failure_recorded(tmp_path: Path) -> None:
    wd = Watchdog(
        target=[sys.executable, "-c", "import sys; sys.exit(7)"],
        lock_path=tmp_path / "wd.pid",
        backoff=_fast_backoff(),
        max_cycles=2,
    )
    wd.run()
    assert wd.backoff.consecutive_failures >= 2


def test_rejects_bad_target(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        Watchdog(target=42, lock_path=tmp_path / "wd.pid")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Watchdog(target=["python", 123], lock_path=tmp_path / "wd.pid")  # type: ignore[list-item]
