"""Size-based log rotation.

Ported literally from the long-running system's PowerShell log rotation.
If the log file is larger than ``max_bytes``, shift ``.N`` files down
(delete the oldest, rename ``.N-1`` to ``.N``, etc.) and rotate the
current log to ``.1``.

Single-writer assumption: only call this when nothing else is writing
to ``log_path``. Safe to call at supervisor startup; unsafe to call
mid-cycle.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union


def rotate_log(
    log_path: Union[str, os.PathLike],
    max_bytes: int = 10 * 1024 * 1024,
    keep: int = 3,
) -> bool:
    """Rotate ``log_path`` if it exceeds ``max_bytes``.

    Returns True if rotation happened, False if it was a no-op.
    Never raises — rotation failures are non-fatal by design (the
    supervisor's job is to keep going, not to die on log housekeeping).
    """
    if max_bytes < 0:
        raise ValueError("max_bytes must be >= 0")
    if keep < 1:
        raise ValueError("keep must be >= 1")

    p = Path(log_path)
    try:
        if not p.exists() or not p.is_file():
            return False
        if p.stat().st_size <= max_bytes:
            return False
    except OSError:
        return False

    # Shift .N down: delete .keep, rename .K-1 -> .K, ..., .1 -> .2
    for i in range(keep, 0, -1):
        src = p.with_name(p.name + f".{i}")
        try:
            if not src.exists():
                continue
            if i == keep:
                src.unlink()
            else:
                dst = p.with_name(p.name + f".{i + 1}")
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
        except OSError:
            # One failed shift shouldn't abort the whole rotation; keep going.
            continue

    # Rotate current to .1
    try:
        rotated = p.with_name(p.name + ".1")
        if rotated.exists():
            rotated.unlink()
        p.rename(rotated)
        return True
    except OSError:
        return False
