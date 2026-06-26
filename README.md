# evergreen-watchdog

A Python process supervisor for **long-running scheduled cycles** — jobs that need to keep running across crashes, sleep/wake, intermittent internet, and host reboots. Pluggable pre-cycle gates (internet probe with fallback, RAM-pressure pause, custom flags), exponential backoff with operator-pause-after-N-failures, singleton lock that survives the host's scheduler firing the supervisor twice on login.

Extracted from a private long-running system that needed to keep a research loop alive across 100+ continuous cycles with zero operator intervention. The patterns here were proven against months of real-world failures — captive-portal Wi-Fi, RAM pressure from foreground apps, scheduler logon-storms, mid-cycle laptop sleeps.

> **Status (v0.1.0):** Source-only release. Not yet published to PyPI. Install from GitHub — see [Install](#install) below. The companion dependency [`racefree-filelock`](https://github.com/aiton08255/racefree-filelock) is also source-only for now; both must be installed from GitHub.

---

## What this is

A `Watchdog` class that wraps your job (a Python callable OR a subprocess command) and:

1. **Guarantees only one supervisor runs** at a time, even if the OS scheduler fires it twice. Uses [racefree-filelock](https://github.com/aiton08255/racefree-filelock) under the hood.
2. **Checks pre-cycle gates** before each invocation. If any gate blocks (no internet, RAM tight, operator paused), the cycle is skipped and retried later.
3. **Backs off exponentially on failure**, capped at `max_sleep`. After N consecutive failures, jumps to a long pause so an operator can inspect instead of the supervisor producing more empty cycles.
4. **Survives the supervisor process dying** — the OS scheduler restarts it, the lock detects the dead PID and takes over safely.
5. **Rotates its own log** at startup so disk usage stays bounded.

Plus templates for **Windows Scheduled Task** and **systemd** to wire it into your host's scheduler.

## What this is NOT — read this first

| If you need... | Use... |
|---|---|
| **A real init system / service manager** | `systemd`, `launchd`, `nssm`, Windows Services. Don't try to replace these. |
| **A general-purpose process supervisor** (multiple services, dependency graphs, web UI) | [`supervisord`](http://supervisord.org/), [`s6`](https://skarnet.org/software/s6/), `pm2` |
| **Container orchestration** | Kubernetes, Nomad, Docker Compose with `restart: always` |
| **A cron replacement with fancy scheduling** | `cron`, `systemd-timer`, `airflow`, `prefect`, `dagster` |
| **Distributed task queues** | Celery, RQ, Sidekiq |
| **A blocking lock around a critical section inside one Python process** | `threading.Lock` |

**Use this when**: you have ONE long-running job on ONE machine, and you need it to keep running across failures with operator-friendly backoff and gating. Typical use cases:

- A research/data loop that should run continuously
- A nightly batch with internet-dependent calls that might find the network down
- A periodic ML training cycle that should pause when RAM is tight
- An ETL job that needs to skip cycles during business hours via a flag file
- Anything you'd otherwise wrap in a hand-rolled `while True: try: ... except: time.sleep(60)` loop

---

## Install

Until v0.1.0 lands on PyPI, install both packages directly from GitHub:

```bash
pip install git+https://github.com/aiton08255/racefree-filelock
pip install git+https://github.com/aiton08255/evergreen-watchdog

# Optional: psutil enables the RAMGate
pip install psutil
```

Or clone and install editable for local hacking:

```bash
git clone https://github.com/aiton08255/racefree-filelock
git clone https://github.com/aiton08255/evergreen-watchdog
pip install -e ./racefree-filelock
pip install -e "./evergreen-watchdog[dev,psutil]"
```

Once published to PyPI (planned), the install will simplify to:

```bash
pip install evergreen-watchdog[psutil]
```

## Use — Python API

```python
from evergreen_watchdog import Watchdog, Backoff, InternetGate, RAMGate, FileFlagGate

def my_cycle() -> int:
    # ...your actual work...
    # return 0 on success, non-zero on failure
    return 0

wd = Watchdog(
    target=my_cycle,
    lock_path="/var/run/myjob.pid",
    gates=[
        InternetGate(),                                # needs internet to run
        RAMGate(max_pct=85, bypass_after_sec=3600),    # pause if RAM>85%, bypass after 1h
        FileFlagGate("/tmp/pause-myjob"),              # `touch` this file to pause
    ],
    backoff=Backoff(initial_sec=60, max_sec=3600, pause_after_n=3),
    log_path="/var/log/myjob-watchdog.log",
)
exit_code = wd.run()  # blocks until SIGINT/SIGTERM or max_cycles reached
```

## Use — CLI

For wrapping an existing command without writing Python:

```bash
evergreen-watchdog \
    --lock /var/run/myjob.pid \
    --log /var/log/myjob-watchdog.log \
    --internet \
    --ram-max 85 \
    --flag-file /tmp/pause-myjob \
    --initial-sleep 60 \
    --max-sleep 3600 \
    --pause-after 3 \
    -- python /opt/myjob/cycle.py --batch-size 100
```

## Use — systemd

See `scripts/evergreen-watchdog.service.template`. Replace the `<PLACEHOLDER>` values and copy to `/etc/systemd/system/myjob-watchdog.service`.

## Use — Windows Scheduled Task

See `scripts/evergreen-watchdog-task.xml.template`. Replace the placeholders and register with `schtasks /Create /TN MyJob-Watchdog /XML myjob-watchdog.xml`.

---

## Gates

A gate is a check that runs **before each cycle**. If it returns `(allowed=False, reason)` the cycle is skipped and the supervisor sleeps `check_interval` seconds before checking again.

| Gate | Purpose | Knobs |
|---|---|---|
| `InternetGate(probes=("1.1.1.1", "9.9.9.9"), timeout=3.0)` | TCP-connect probe; needs at least one host to be reachable. Port 53 (DNS) by default. | Multi-host fallback survives single-resolver outages. |
| `RAMGate(max_pct=85.0, bypass_after_sec=3600.0)` | Block when memory usage exceeds threshold. Bypass after N seconds blocked so a wedged process doesn't pause the supervisor forever. Requires `psutil`. | If `psutil` is missing, gate ALWAYS allows the cycle (fails open). |
| `FileFlagGate(path)` | Block while a flag file exists. Operator does `touch /tmp/pause` to pause, `rm /tmp/pause` to resume. | Tiny, robust, easy to script. |
| `EnvVarGate(name, blocking_value=None)` | Block when an env var is set (any value, or a specific blocking value). | Useful for "pause this systemd unit" via `systemctl edit`. |

### Custom gates

Anything with `name: str` and `check() -> (bool, str)` works:

```python
class BusinessHoursGate:
    name = "business-hours"
    def check(self):
        from datetime import datetime
        h = datetime.now().hour
        if 9 <= h < 17:
            return False, f"business hours ({h}:00)"
        return True, ""

wd = Watchdog(target=my_cycle, lock_path=..., gates=[BusinessHoursGate()])
```

A gate that raises an exception is treated as **blocking** (fail-safe). The exception is logged.

---

## Backoff

```python
Backoff(initial_sec=60, max_sec=3600, pause_after_n=3, pause_sec=24*3600)
```

- After a **success**: sleep `initial_sec`.
- After failure **N**: sleep `min(initial_sec * 2^(N-1), max_sec)`.
- After **pause_after_n** consecutive failures: sleep jumps to `pause_sec` (default 24h). The supervisor stays alive but waits long enough that an operator will see and investigate before more empty cycles produce more noise.

A clean cycle resets the failure counter.

---

## Limitations — the honest list

1. **Not a replacement for systemd / init systems.** This supervises ONE job and assumes something else (systemd, Scheduled Task, launchd) restarts the supervisor itself if it dies. The included templates wire that up; without one, an `OOM kill` of the supervisor leaves your job unsupervised until next login/reboot.

2. **One job per supervisor.** No dependency graphs, no service trees. If you need that, use `supervisord` or systemd.

3. **No retry budget separate from time.** Backoff is time-based, not attempt-based. If your job fails 100 times in 5 minutes, the supervisor will run all 100 — though each one waits longer than the last per the exponential schedule. There's no "max 10 attempts then give up."

4. **`RAMGate` bypass is process-blind.** If memory is held by ONE wedged process, the bypass still kicks in and runs your job, which may OOM. The gate is a heuristic, not a guarantee. If you need real memory pressure handling, use cgroups / Job Objects.

5. **`InternetGate` proves only that *some* host is reachable.** Captive portals can intercept TCP/53 in some configurations. If your job has a specific endpoint, write a custom gate that probes it.

6. **Singleton lock has the same caveats as [racefree-filelock](https://github.com/aiton08255/racefree-filelock).** Not safe on NFS or shared filesystems. PID reuse on long-uptime Windows systems can produce false-positives ("a different process now has that PID"); the `identity` parameter fixes this when `psutil` is installed.

7. **Subprocess targets don't get a stop signal yet.** If the supervisor is killed mid-cycle, the subprocess child is left running until it exits on its own. v0.2 will add `terminate_on_stop`.

8. **Log rotation is startup-only.** If your supervisor never restarts, the log can grow unbounded across the rotation threshold. Practical mitigation: pair this with `logrotate` (Linux) or rely on the supervisor restart cadence.

9. **Gates check before the cycle, not during.** If the network drops mid-cycle, the supervisor doesn't kill the job. Your job is responsible for its own timeouts.

10. **27 tests cover the supervisor logic, gates, backoff, and rotation. They do NOT cover: NFS, slow-starting subprocess targets that swallow SIGTERM, graceful shutdown during a multi-hour `pause_sec` sleep, or Windows Job Object integration.** Bug reports welcome.

---

## Alternatives — when NOT to use this

| Tool | Use it instead when |
|---|---|
| **systemd** | You're on Linux and want full init-system features (dependencies, sockets, journal integration, full hardening). |
| **supervisord** | You're supervising multiple processes that need to start/stop together. |
| **nssm** | You want to run a Windows service without writing a Service host. |
| **pm2** | You're in Node.js land. |
| **cron / systemd-timer** | Your job is a quick batch (seconds to minutes) not a continuous cycle. |
| **k8s / Nomad** | You're already in a container orchestrator — let it do the supervision. |
| **A try/except + time.sleep loop** | Your job is small enough that pulling in a dependency isn't worth it. The whole point of this library is that it stays small (~600 LOC of source). |

---

## API reference

### `Watchdog(target, lock_path, ...)`

| Parameter | Type | Default | Purpose |
|---|---|---|---|
| `target` | `Callable[[], int]` or `list[str]` | required | The job to run each cycle. |
| `lock_path` | `str` or `Path` | required | Singleton lock file. |
| `gates` | `list[Gate]` | `[]` | Pre-cycle checks. |
| `backoff` | `Backoff` | `Backoff()` | Sleep policy. |
| `check_interval` | `float` | `60.0` | Seconds between gate-blocked retries. |
| `log_path` | `str` or `Path` or `None` | `None` | Optional FileHandler attached to the supervisor's own log. |
| `log_max_bytes` | `int` | `10_485_760` (10 MiB) | Rotation threshold. |
| `log_keep` | `int` | `3` | Number of rotated logs to keep. |
| `identity` | `str` or `None` | `None` | Passed to `FileLock` for identity-aware liveness. |
| `max_cycles` | `int` or `None` | `None` | If set, exit after N cycles. For tests. |

### `Watchdog.run() -> int`

Blocks until graceful shutdown. Returns `0` on clean exit, `75` (POSIX `EX_TEMPFAIL`) if the singleton lock is held by a live peer.

### `Watchdog.stop()`

Request graceful shutdown. Safe from a signal handler or another thread. The current cycle finishes; sleep is interrupted within 1 second.

### `Backoff(initial_sec=60, max_sec=3600, pause_after_n=3, pause_sec=86400)`

See the Backoff section above.

### Gates

See the Gates section above for the four built-ins. Custom gates need `name: str` and `check() -> tuple[bool, str]`.

### `rotate_log(log_path, max_bytes=10*1024*1024, keep=3) -> bool`

Exposed for direct use if you want rotation outside the supervisor.

---

## Run the tests

```bash
git clone https://github.com/aiton08255/evergreen-watchdog
cd evergreen-watchdog
pip install -e ".[dev,psutil]"
python -m pytest tests/ -v
```

Current status: **27 passed in ~10s** on Python 3.12 / Windows 11.

The key tests:
- `test_singleton_lock_prevents_second_supervisor` — spawns a real subprocess holding the lock; verifies a second supervisor exits with code 75 (cross-process correctness).
- `test_blocking_gate_skips_cycle_then_runs_when_unblocked` — opens a flag file from a background thread; verifies the supervisor skips while blocked and runs when the gate opens.
- `test_failing_target_triggers_backoff_then_pause` — checks the exponential schedule actually escalates wall-clock sleep.
- `test_stop_request_exits_cleanly_mid_sleep` — verifies `stop()` interrupts a multi-second sleep within ~1 second.

---

## Provenance

Battle-tested across 100+ continuous research cycles in a private long-running system. Survived: Wi-Fi outages, captive portals on cellular fallback, RAM pressure from gaming sessions, Windows Scheduled Task logon-storms (4 spawn attempts in 64s on user login), mid-cycle laptop sleeps, and the supervisor process being SIGKILL'd 12+ times.

The private system this came from is not open-source. The patterns are published standalone because anyone writing a `while True: try: ...` loop will eventually re-derive most of them, and the lock-race fix specifically is non-obvious.

---

## License

Apache 2.0. See `LICENSE`.

## Contributing

Keep the LOC count low. The point of this library is that it's small enough to read and verify in one sitting. PRs welcome for:

- Real bugs (with a reproducer)
- New built-in gates that have proven themselves in production (not just "would be nice")
- Cross-platform fixes for Linux/macOS edge cases (the original system runs on Windows; Linux is well-covered by tests but less by production hours)

Open an [issue](https://github.com/aiton08255/evergreen-watchdog/issues) before a large PR.
