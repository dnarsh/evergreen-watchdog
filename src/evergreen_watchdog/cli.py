"""Minimal CLI for running a subprocess command under the watchdog.

Usage:
    evergreen-watchdog --lock /tmp/myjob.pid -- my-script --arg1 value1

    evergreen-watchdog \\
        --lock /tmp/myjob.pid \\
        --log /var/log/myjob.log \\
        --internet \\
        --ram-max 85 \\
        --max-cycles 10 \\
        -- python my_cycle.py
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional, Sequence

from evergreen_watchdog.backoff import Backoff
from evergreen_watchdog.gates import (
    EnvVarGate,
    FileFlagGate,
    Gate,
    InternetGate,
    RAMGate,
)
from evergreen_watchdog.watchdog import Watchdog


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="evergreen-watchdog",
        description="Run a command under a long-running process supervisor.",
    )
    p.add_argument("--lock", required=True, help="Path to singleton lock file")
    p.add_argument("--log", default=None, help="Path to supervisor log file")
    p.add_argument(
        "--check-interval",
        type=float,
        default=60.0,
        help="Seconds between gate-blocked retries (default 60)",
    )
    p.add_argument(
        "--initial-sleep",
        type=float,
        default=60.0,
        help="Sleep after a clean cycle (default 60s)",
    )
    p.add_argument(
        "--max-sleep",
        type=float,
        default=3600.0,
        help="Cap on exponential backoff (default 1h)",
    )
    p.add_argument(
        "--pause-after",
        type=int,
        default=3,
        help="Long-pause after this many consecutive failures (default 3)",
    )
    p.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="Exit after N cycles (default: run forever)",
    )
    p.add_argument(
        "--identity",
        default=None,
        help="Substring for racefree-filelock identity-aware liveness",
    )

    g = p.add_argument_group("gates")
    g.add_argument(
        "--internet",
        action="store_true",
        help="Add InternetGate (probes 1.1.1.1, 9.9.9.9 on port 53)",
    )
    g.add_argument(
        "--ram-max",
        type=float,
        default=None,
        help="Add RAMGate with this max percentage (e.g. 85)",
    )
    g.add_argument(
        "--ram-bypass-after",
        type=float,
        default=3600.0,
        help="RAMGate bypass interval in seconds (default 1h)",
    )
    g.add_argument(
        "--flag-file",
        action="append",
        default=[],
        help="Add a FileFlagGate (repeat for multiple flag files)",
    )
    g.add_argument(
        "--env-flag",
        action="append",
        default=[],
        help="Add an EnvVarGate (repeat for multiple env vars)",
    )

    p.add_argument(
        "command", nargs=argparse.REMAINDER, help="Command to run (prefix with --)"
    )
    return p


def _build_gates(args: argparse.Namespace) -> List[Gate]:
    gates: List[Gate] = []
    if args.internet:
        gates.append(InternetGate())
    if args.ram_max is not None:
        gates.append(
            RAMGate(max_pct=args.ram_max, bypass_after_sec=args.ram_bypass_after)
        )
    for path in args.flag_file:
        gates.append(FileFlagGate(path))
    for name in args.env_flag:
        gates.append(EnvVarGate(name))
    return gates


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        parser.error("a command is required after --")

    backoff = Backoff(
        initial_sec=args.initial_sleep,
        max_sec=args.max_sleep,
        pause_after_n=args.pause_after,
    )
    wd = Watchdog(
        target=cmd,
        lock_path=args.lock,
        gates=_build_gates(args),
        backoff=backoff,
        check_interval=args.check_interval,
        log_path=args.log,
        identity=args.identity,
        max_cycles=args.max_cycles,
    )
    return wd.run()


if __name__ == "__main__":
    sys.exit(main())
