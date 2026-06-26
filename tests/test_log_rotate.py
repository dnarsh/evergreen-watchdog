"""Log rotation tests — real fs only."""

from __future__ import annotations

from pathlib import Path

import pytest

from evergreen_watchdog import rotate_log


def test_no_op_when_under_threshold(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    log.write_text("small")
    rotated = rotate_log(log, max_bytes=100, keep=3)
    assert rotated is False
    assert log.exists()
    assert not (tmp_path / "app.log.1").exists()


def test_rotation_creates_dot1_and_clears_current(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    log.write_text("x" * 200)
    rotated = rotate_log(log, max_bytes=100, keep=3)
    assert rotated is True
    assert not log.exists()
    assert (tmp_path / "app.log.1").exists()
    assert (tmp_path / "app.log.1").read_text() == "x" * 200


def test_rotation_shifts_existing_numbered_files_down(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    (tmp_path / "app.log.1").write_text("OLDER-1")
    (tmp_path / "app.log.2").write_text("OLDER-2")
    log.write_text("X" * 500)

    rotated = rotate_log(log, max_bytes=100, keep=3)

    assert rotated is True
    assert (tmp_path / "app.log.1").read_text() == "X" * 500
    assert (tmp_path / "app.log.2").read_text() == "OLDER-1"
    assert (tmp_path / "app.log.3").read_text() == "OLDER-2"


def test_rotation_drops_oldest_when_keep_exceeded(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    (tmp_path / "app.log.1").write_text("OLDER-1")
    (tmp_path / "app.log.2").write_text("OLDER-2")
    (tmp_path / "app.log.3").write_text("OLDEST")  # this one should be dropped
    log.write_text("X" * 500)

    rotated = rotate_log(log, max_bytes=100, keep=3)

    assert rotated is True
    assert (tmp_path / "app.log.1").read_text() == "X" * 500
    assert (tmp_path / "app.log.2").read_text() == "OLDER-1"
    assert (tmp_path / "app.log.3").read_text() == "OLDER-2"
    # The original .3 ("OLDEST") was dropped


def test_rejects_bad_args(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        rotate_log(tmp_path / "x", max_bytes=-1)
    with pytest.raises(ValueError):
        rotate_log(tmp_path / "x", max_bytes=100, keep=0)


def test_missing_log_is_silent_noop(tmp_path: Path) -> None:
    rotated = rotate_log(tmp_path / "nonexistent.log", max_bytes=100, keep=3)
    assert rotated is False
