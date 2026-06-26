"""The Watchdog supervisor.

Runs a target (callable or subprocess command) on a loop, with:
  * Singleton protection via racefree-filelock (prevents double-spawn
    when the host's scheduler fires the supervisor twice on login).
  * Pluggable pre-cycle gates (internet, RAM, custom).
  * Exponential backoff with pause-after-N consecutive failures.
  * Built-in log rotation at startup.
  * Graceful shutdown on SIGINT/SIGTERM.

Target contract:
  * Callable: any ``Callable[[], int]``. Return 0 for success, non-zero
    for failure. Raised exceptions count as failure.
  * Subprocess: ``list[str]`` passed to ``subprocess.run``. Non-zero
    exit code counts as failure.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Union

from racefree_filelock import FileLock, LockHeld

from evergreen_watchdog.backoff import Backoff
from evergreen_watchdog.gates import Gate
from evergreen_watchdog.log_rotate import rotate_log

TargetCallable = Callable[[], int]
TargetCommand = Sequence[str]
Target = Union[TargetCallable, TargetCommand]


class Watchdog:
    """Long-running process supervisor.

    Args:
        target: Either a zero-arg callable returning an int exit code,
            or a list[str] subprocess command. Anything else raises
            ``TypeError``.
        lock_path: Path to the singleton lock file. Required — without
            it the supervisor cannot prevent double-spawn from a
            scheduler firing twice.
        gates: List of pre-cycle gates. Empty list (default) means no
            gating. Gates are checked in order; first blocking gate
            wins and the cycle is skipped.
        backoff: Backoff state machine. Defaults to
            ``Backoff(initial_sec=60, max_sec=3600, pause_after_n=3)``.
        check_interval: Seconds to sleep between gate-blocked retries.
            Does not affect post-cycle sleep, which is governed by
            ``backoff``.
        log_path: Optional path for supervisor's own log. If given, a
            FileHandler is attached and the log is rotated at startup.
        log_max_bytes / log_keep: Rotation parameters for ``log_path``.
        identity: Passed to racefree-filelock so the lock distinguishes
            this supervisor from unrelated processes that happened to
            inherit the same PID.
        max_cycles: If set, run at most this many cycles then return.
            For tests. None (default) = run forever.
    """

    def __init__(
        self,
        target: Target,
        lock_path: Union[str, Path],
        gates: Optional[List[Gate]] = None,
        backoff: Optional[Backoff] = None,
        check_interval: float = 60.0,
        log_path: Optional[Union[str, Path]] = None,
        log_max_bytes: int = 10 * 1024 * 1024,
        log_keep: int = 3,
        identity: Optional[str] = None,
        max_cycles: Optional[int] = None,
    ) -> None:
        if not callable(target) and not (
            isinstance(target, (list, tuple))
            and all(isinstance(x, str) for x in target)
        ):
            raise TypeError("target must be a Callable[[], int] or a list[str] command")
        if check_interval <= 0:
            raise ValueError("check_interval must be positive")

        self.target = target
        self.lock_path = Path(lock_path)
        self.gates = list(gates or [])
        self.backoff = backoff or Backoff()
        self.check_interval = float(check_interval)
        self.log_path = Path(log_path) if log_path else None
        self.log_max_bytes = log_max_bytes
        self.log_keep = log_keep
        self.identity = identity
        self.max_cycles = max_cycles

        self._stop_requested = False
        self._cycle_count = 0
        self._logger = self._build_logger()

    def _build_logger(self) -> logging.Logger:
        log = logging.getLogger(f"evergreen_watchdog[{self.lock_path.name}]")
        log.setLevel(logging.INFO)
        if not log.handlers:
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(
                logging.Formatter("[%(asctime)s][%(levelname)s] %(message)s")
            )
            log.addHandler(stream)
            if self.log_path is not None:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                fh = logging.FileHandler(self.log_path, encoding="utf-8")
                fh.setFormatter(
                    logging.Formatter("[%(asctime)s][%(levelname)s] %(message)s")
                )
                log.addHandler(fh)
        return log

    def stop(self, *_args) -> None:
        """Request graceful shutdown. Safe to call from a signal handler
        or another thread."""
        self._stop_requested = True
        self._logger.info("stop requested")

    def _install_signal_handlers(self) -> None:
        # Signals are only safe to install from the main thread; quietly
        # skip if we can't (e.g. running inside a thread pool worker).
        try:
            signal.signal(signal.SIGINT, self.stop)
            signal.signal(signal.SIGTERM, self.stop)
        except (ValueError, OSError):
            pass

    def _check_gates(self) -> Optional[str]:
        """Return None if all gates pass, else the reason for the first
        blocking gate."""
        for g in self.gates:
            try:
                allowed, reason = g.check()
            except Exception as e:
                # A gate that crashes is treated as blocking — fail safe.
                return f"gate {g.name!r} raised {type(e).__name__}: {e}"
            if not allowed:
                return f"gate {g.name!r}: {reason}"
        return None

    def _run_target_once(self) -> int:
        """Invoke the target. Returns exit code (0 = success). Exceptions
        from a callable target are caught and logged as exit code 1."""
        if callable(self.target):
            try:
                rc = self.target()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                self._logger.error(f"target raised {type(e).__name__}: {e}")
                return 1
            if not isinstance(rc, int):
                self._logger.warning(
                    f"target returned non-int {type(rc).__name__}; treating as failure"
                )
                return 1
            return rc
        # subprocess command
        try:
            proc = subprocess.run(list(self.target))
            return int(proc.returncode)
        except FileNotFoundError as e:
            self._logger.error(f"target executable not found: {e}")
            return 127
        except OSError as e:
            self._logger.error(f"target subprocess failed: {e}")
            return 1

    def _sleep_interruptible(self, seconds: float) -> bool:
        """Sleep in 1-second slices so stop()/SIGINT lands quickly.
        Returns False if interrupted, True if slept the whole duration."""
        slept = 0.0
        while slept < seconds:
            if self._stop_requested:
                return False
            chunk = min(1.0, seconds - slept)
            try:
                time.sleep(chunk)
            except KeyboardInterrupt:
                self._stop_requested = True
                return False
            slept += chunk
        return True

    def run(self) -> int:
        """Run the supervisor loop.

        Returns 0 on graceful shutdown, 75 if the singleton lock is held
        by a live peer (chosen to match POSIX EX_TEMPFAIL conventions).
        """
        self._install_signal_handlers()

        if self.log_path is not None:
            rotated = rotate_log(
                self.log_path, max_bytes=self.log_max_bytes, keep=self.log_keep
            )
            if rotated:
                self._logger.info(f"rotated log {self.log_path}")

        lock = FileLock(self.lock_path, identity=self.identity, register_atexit=False)
        try:
            lock.acquire()
        except LockHeld as e:
            self._logger.warning(
                f"another supervisor (PID {e.holder_pid}) holds {self.lock_path} — exiting"
            )
            return 75

        try:
            self._logger.info(
                f"supervisor started (lock={self.lock_path}, gates={[g.name for g in self.gates]})"
            )
            return self._main_loop()
        finally:
            lock.release()
            self._logger.info("supervisor stopped")

    def _main_loop(self) -> int:
        while not self._stop_requested:
            if self.max_cycles is not None and self._cycle_count >= self.max_cycles:
                self._logger.info(f"reached max_cycles={self.max_cycles}; exiting")
                return 0

            block_reason = self._check_gates()
            if block_reason is not None:
                self._logger.info(f"gate blocked cycle: {block_reason}")
                if not self._sleep_interruptible(self.check_interval):
                    break
                continue

            self._cycle_count += 1
            self._logger.info(f"cycle {self._cycle_count} starting")
            started = time.monotonic()
            try:
                rc = self._run_target_once()
            except KeyboardInterrupt:
                self._stop_requested = True
                break
            elapsed = time.monotonic() - started

            if rc == 0:
                sleep_s = self.backoff.success()
                self._logger.info(
                    f"cycle {self._cycle_count} done in {elapsed:.1f}s "
                    f"(rc=0, next sleep {sleep_s:.0f}s)"
                )
            else:
                sleep_s = self.backoff.failure()
                self._logger.warning(
                    f"cycle {self._cycle_count} FAILED in {elapsed:.1f}s "
                    f"(rc={rc}, consecutive_failures={self.backoff.consecutive_failures}, "
                    f"next sleep {sleep_s:.0f}s, paused={self.backoff.paused})"
                )

            if not self._sleep_interruptible(sleep_s):
                break

        return 0

    @property
    def cycle_count(self) -> int:
        return self._cycle_count
