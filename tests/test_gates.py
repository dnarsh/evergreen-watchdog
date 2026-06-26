"""Gate tests — exercise the real check() path with real fs / sockets
where feasible, and a controlled clock for time-dependent logic."""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from evergreen_watchdog import EnvVarGate, FileFlagGate, InternetGate, RAMGate


def test_file_flag_gate_blocks_when_file_present(tmp_path: Path) -> None:
    flag = tmp_path / "pause.flag"
    g = FileFlagGate(flag)

    allowed, _ = g.check()
    assert allowed is True

    flag.write_text("")
    allowed, reason = g.check()
    assert allowed is False
    assert "pause.flag" in reason

    flag.unlink()
    allowed, _ = g.check()
    assert allowed is True


def test_env_var_gate_blocks_when_set_unsets_when_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MY_TEST_GATE", raising=False)
    g = EnvVarGate("MY_TEST_GATE")

    allowed, _ = g.check()
    assert allowed is True

    monkeypatch.setenv("MY_TEST_GATE", "1")
    allowed, reason = g.check()
    assert allowed is False
    assert "MY_TEST_GATE" in reason


def test_env_var_gate_specific_blocking_value(monkeypatch: pytest.MonkeyPatch) -> None:
    g = EnvVarGate("MY_TEST_GATE", blocking_value="stop")
    monkeypatch.setenv("MY_TEST_GATE", "go")
    allowed, _ = g.check()
    assert allowed is True
    monkeypatch.setenv("MY_TEST_GATE", "stop")
    allowed, _ = g.check()
    assert allowed is False


def _serve_once(port_holder: list, ready: threading.Event) -> None:
    """Bind a real TCP socket on an ephemeral port and accept one connection."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port_holder.append(s.getsockname()[1])
    ready.set()
    try:
        conn, _ = s.accept()
        conn.close()
    finally:
        s.close()


def test_internet_gate_passes_when_at_least_one_probe_reachable() -> None:
    """Real socket-level test — no mock. We bind a real TCP server,
    then ask InternetGate to probe it (alongside a deliberately
    unreachable address). The fallback should make it through."""
    port_holder: list = []
    ready = threading.Event()
    t = threading.Thread(target=_serve_once, args=(port_holder, ready), daemon=True)
    t.start()
    assert ready.wait(timeout=5), "server failed to bind"
    port = port_holder[0]

    g = InternetGate(
        probes=[
            ("198.51.100.1", 53),
            ("127.0.0.1", port),
        ],  # first is RFC 5737 TEST-NET-2 (unrouteable)
        timeout=2.0,
    )
    allowed, _ = g.check()
    assert allowed is True
    t.join(timeout=2)


def test_internet_gate_fails_when_all_probes_unreachable() -> None:
    g = InternetGate(
        probes=[("198.51.100.1", 53), ("198.51.100.2", 53)],
        timeout=0.5,
    )
    allowed, reason = g.check()
    assert allowed is False
    assert "198.51.100" in reason


def test_ram_gate_passes_under_threshold_blocks_over_then_bypasses() -> None:
    """Inject a controlled clock; force psutil to report a specific
    memory percentage by monkey-patching virtual_memory."""
    psutil = pytest.importorskip("psutil")

    fake_now = [0.0]
    fake_pct = [90.0]  # over default threshold

    class FakeMem:
        percent = fake_pct[0]

    def fake_vmem():
        m = FakeMem()
        m.percent = fake_pct[0]
        return m

    real_vmem = psutil.virtual_memory
    psutil.virtual_memory = fake_vmem
    try:
        g = RAMGate(
            max_pct=85.0, bypass_after_sec=10.0, time_source=lambda: fake_now[0]
        )

        # First block records the start time
        allowed, _ = g.check()
        assert allowed is False

        # Still under the bypass window — keeps blocking
        fake_now[0] = 5.0
        allowed, _ = g.check()
        assert allowed is False

        # Cross the bypass window — one pass-through
        fake_now[0] = 11.0
        allowed, reason = g.check()
        assert allowed is True
        assert "bypass" in reason

        # After bypass, the timer is reset — would block again immediately
        allowed, _ = g.check()
        assert allowed is False

        # When RAM drops back under threshold, gate passes cleanly
        fake_pct[0] = 50.0
        allowed, _ = g.check()
        assert allowed is True
    finally:
        psutil.virtual_memory = real_vmem


def test_gate_rejects_bad_args() -> None:
    with pytest.raises(ValueError):
        InternetGate(probes=[], timeout=1)
    with pytest.raises(ValueError):
        InternetGate(probes=["1.1.1.1"], timeout=0)
    with pytest.raises(ValueError):
        RAMGate(max_pct=0)
    with pytest.raises(ValueError):
        RAMGate(max_pct=100)
    with pytest.raises(ValueError):
        EnvVarGate("")
