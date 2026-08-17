"""Timelock format preflight — does THIS install speak the consensus format?

The subnet's LF competition is the only one gated on opening a drand-tlock
reveal, and two mutually-unreadable ciphertext formats exist in the wild:

    timelock_wasm_wrapper 0.3.0  -> 245-byte W_time   <- CONSENSUS (what the
                                                         live fleet seals)
    timelock_wasm_wrapper 0.0.2  -> 261-byte W_time   <- legacy (PyPI latest)

A validator holding only the legacy version opens NOTHING the live fleet seals.
It does not crash and it does not warn: every LF reveal simply fails to open, no
miner accumulates the QUALIFY_MIN_DECISIVE graded calls inside the 7-day
EMISSION_DECAY_S window, the LF earner set goes empty, and combine()'s
dead-share rule burns LF's whole COMP_WEIGHTS share. The operator sees only a
collapsing vtrust. That silence is what this check exists to break.

Run it any time:  python tools/check_timelock.py
"""
from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sn89_signals import config  # noqa: E402

CONSENSUS_W_TIME_LEN = 245
LEGACY_W_TIME_LEN = 261

_PROBE = """
import sys
from timelock import Timelock
tl = Timelock(sys.argv[1])
print(len(tl.tle(1000, "sn89-format-probe", b"\\x00" * 32)))
"""


def sealed_len(python_exe: str) -> int | None:
    """Bytes of W_time this interpreter produces, or None if it cannot seal."""
    try:
        r = subprocess.run([python_exe, "-c", _PROBE, config.DRAND_PUBLIC_KEY],
                           capture_output=True, text=True, timeout=60)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def describe(n: int | None) -> str:
    if n is None:
        return "cannot seal (timelock not importable)"
    if n == CONSENSUS_W_TIME_LEN:
        return f"{n}B — CONSENSUS format"
    if n == LEGACY_W_TIME_LEN:
        return f"{n}B — legacy format"
    return f"{n}B — UNKNOWN format"


def report() -> int:
    """Print the opener inventory. Returns a process exit code."""
    primary = sealed_len(sys.executable)
    fallback_py = os.getenv("SN89_TLD_FALLBACK_PYTHON", "")
    fallback = sealed_len(fallback_py) if fallback_py and os.path.exists(fallback_py) else None

    print("timelock opener inventory")
    print(f"  primary  ({sys.executable}): {describe(primary)}")
    if fallback_py:
        where = fallback_py if os.path.exists(fallback_py) else f"{fallback_py} (MISSING)"
        print(f"  fallback ({where}): {describe(fallback)}")
    else:
        print("  fallback: not configured (SN89_TLD_FALLBACK_PYTHON unset)")

    openable = {n for n in (primary, fallback) if n is not None}
    if CONSENSUS_W_TIME_LEN not in openable:
        print()
        print("  ✗ FATAL: this install cannot open the format the live fleet seals.")
        print("    Every LF reveal will fail to open, the LF earner set will go")
        print("    empty, and LF's whole COMP_WEIGHTS share will burn — silently.")
        print("    Fix: pip install vendor/timelock/*.whl  (see README, 'Timelock")
        print("    version compatibility').")
        return 1
    if LEGACY_W_TIME_LEN not in openable:
        print()
        print("  ⚠ consensus format OK, but the legacy opener is absent: reveals")
        print("    sealed by a miner on timelock_wasm_wrapper 0.0.2 will void.")
        print("    Replaying history needs both. See the README section above.")
        return 0
    print()
    print("  ✓ both formats openable — LF grading and historical replay are safe.")
    return 0


def warn_if_broken(emit=print) -> None:
    """Startup guard for the validator. Log-only: never changes the vector."""
    try:
        if sealed_len(sys.executable) == CONSENSUS_W_TIME_LEN:
            return
        fb = os.getenv("SN89_TLD_FALLBACK_PYTHON", "")
        if fb and os.path.exists(fb) and sealed_len(fb) == CONSENSUS_W_TIME_LEN:
            return
        emit("  ✗✗ TIMELOCK FORMAT MISMATCH — cannot open the format the live "
             "fleet seals. ALL LF reveals will void and LF's share will BURN. "
             "Run: python tools/check_timelock.py")
    except Exception:  # noqa: BLE001 — a preflight must never stop the validator
        pass


if __name__ == "__main__":
    raise SystemExit(report())
