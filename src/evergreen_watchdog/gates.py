"""Pre-cycle gates.

A gate is anything with ``name: str`` and ``check() -> (allowed, reason)``.
If any gate returns ``allowed=False`` the watchdog skips the cycle and
sleeps until the next check interval. Reason is logged for operator
visibility.

Built-in gates encode patterns extracted from a long-running production
supervisor:

* ``InternetGate``     — multi-probe TCP fallback (1.1.1.1 + 9.9.9.9 by
                         default). Survives single-resolver outages.
* ``RAMGate``          — pause when RAM tight; bypass after N seconds so
                         a wedged background process doesn't pause the
                         supervisor forever.
* ``FileFlagGate``     — pause while a flag file exists. Lets an
                         operator say "don't run cycles right now" by
                         touching a file (the original system used this
                         for "game mode" — don't burn CPU while I game).
* ``EnvVarGate``       — same idea, env-var-driven.
"""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path
from typing import Iterable, Optional, Protocol, Tuple, Union


class Gate(Protocol):
    """Pre-cycle gate protocol."""

    name: str

    def check(self) -> Tuple[bool, str]:
        """Return (allowed, reason). allowed=True means the cycle may
        run. reason is informational, empty string when allowed."""
        ...


class InternetGate:
    """TCP-connect probe with fallback host list.

    Tries each host:port in order. If any succeeds within ``timeout``
    seconds the gate passes. Port 53 (DNS) is used by default because
    the canonical fallback hosts (1.1.1.1, 9.9.9.9, 8.8.8.8) all run
    DNS on it. Picks the same probe semantics as the original system
    where ICMP ping was unreliable behind some captive portals but
    TCP/53 went through.

    Pure stdlib — no external dependency on requests/httpx.
    """

    def __init__(
        self,
        probes: Iterable[Union[str, Tuple[str, int]]] = ("1.1.1.1", "9.9.9.9"),
        timeout: float = 3.0,
    ) -> None:
        self.probes = [self._normalize(p) for p in probes]
        if not self.probes:
            raise ValueError("at least one probe required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout
        self.name = "internet"

    @staticmethod
    def _normalize(p: Union[str, Tuple[str, int]]) -> Tuple[str, int]:
        if isinstance(p, tuple):
            return (p[0], int(p[1]))
        return (p, 53)

    def _reachable(self, host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=self.timeout):
                return True
        except (OSError, socket.timeout):
            return False

    def check(self) -> Tuple[bool, str]:
        for host, port in self.probes:
            if self._reachable(host, port):
                return True, ""
        names = ", ".join(f"{h}:{p}" for h, p in self.probes)
        return False, f"no internet (none of {names} reachable within {self.timeout}s)"


class RAMGate:
    """Pause cycles when RAM usage is over ``max_pct``.

    Bypass mechanism: if the gate has been blocking continuously for
    ``bypass_after_sec`` seconds, the next check returns allowed=True
    once (and resets the timer). This is the literal pattern from the
    long-running system — without bypass, a wedged background process
    could keep the supervisor paused forever.

    If ``psutil`` is not installed the gate ALWAYS allows the cycle
    (and the reason includes a note). The supervisor's job is to make
    progress; failing closed on a missing optional dependency is
    worse than running and hitting an OOM.
    """

    def __init__(
        self,
        max_pct: float = 85.0,
        bypass_after_sec: float = 3600.0,
        time_source=time.monotonic,
    ) -> None:
        if not (0 < max_pct < 100):
            raise ValueError("max_pct must be in (0, 100)")
        if bypass_after_sec < 0:
            raise ValueError("bypass_after_sec must be >= 0")
        self.max_pct = max_pct
        self.bypass_after = bypass_after_sec
        self._first_block_at: Optional[float] = None
        self._time = time_source
        self.name = "ram"

    def check(self) -> Tuple[bool, str]:
        try:
            import psutil
        except ImportError:
            return True, "ram-gate: psutil not installed, skipping check"

        pct = psutil.virtual_memory().percent
        if pct <= self.max_pct:
            self._first_block_at = None
            return True, ""

        now = self._time()
        if self._first_block_at is None:
            self._first_block_at = now
            return False, f"ram at {pct:.1f}% > {self.max_pct:.1f}%"

        elapsed = now - self._first_block_at
        if elapsed >= self.bypass_after:
            self._first_block_at = None
            return True, (
                f"ram still at {pct:.1f}% > {self.max_pct:.1f}% but "
                f"bypass after {elapsed:.0f}s >= {self.bypass_after:.0f}s"
            )
        return False, (
            f"ram at {pct:.1f}% > {self.max_pct:.1f}% "
            f"(blocked {elapsed:.0f}s of {self.bypass_after:.0f}s before bypass)"
        )


class FileFlagGate:
    """Pause cycles while a flag file exists.

    Operator API: ``touch /tmp/pause-watchdog`` to pause, ``rm`` to resume.
    Encoded as the original system's "game mode" gate.
    """

    def __init__(self, path: Union[str, os.PathLike]) -> None:
        self.path = Path(path)
        self.name = f"file-flag:{self.path.name}"

    def check(self) -> Tuple[bool, str]:
        if self.path.exists():
            return False, f"flag file {self.path} exists"
        return True, ""


class EnvVarGate:
    """Pause cycles while an env var is set.

    Args:
        name: Env var name.
        blocking_value: If None (default), any non-empty value blocks.
            If a string, only that exact value blocks (other values
            allow the cycle).
    """

    def __init__(self, name: str, blocking_value: Optional[str] = None) -> None:
        if not name:
            raise ValueError("env var name required")
        self.var_name = name
        self.blocking_value = blocking_value
        self.name = f"env:{name}"

    def check(self) -> Tuple[bool, str]:
        val = os.environ.get(self.var_name)
        if val is None or val == "":
            return True, ""
        if self.blocking_value is None:
            return False, f"env {self.var_name}={val!r} set"
        if val == self.blocking_value:
            return False, f"env {self.var_name}={val!r} (matches blocking value)"
        return True, ""
