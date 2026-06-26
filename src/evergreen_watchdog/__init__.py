"""evergreen-watchdog — process supervisor for long-running scheduled cycles.

Public API:
    Watchdog        — the main supervisor
    Backoff         — exponential backoff with pause-after-N-failures
    rotate_log      — size-based log rotation

Gates (pre-cycle checks):
    Gate            — protocol
    InternetGate    — TCP-connect probe with fallback host list
    RAMGate         — pause when RAM tight, bypass after N seconds
    FileFlagGate    — pause when a flag file exists
    EnvVarGate      — pause when an env var is set
"""

from evergreen_watchdog.backoff import Backoff
from evergreen_watchdog.gates import (
    EnvVarGate,
    FileFlagGate,
    Gate,
    InternetGate,
    RAMGate,
)
from evergreen_watchdog.log_rotate import rotate_log
from evergreen_watchdog.watchdog import Watchdog

__all__ = [
    "Backoff",
    "EnvVarGate",
    "FileFlagGate",
    "Gate",
    "InternetGate",
    "RAMGate",
    "Watchdog",
    "rotate_log",
]
__version__ = "0.1.0"
