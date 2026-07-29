"""Regression guard for the anchor sweep's settle margin.

THE INCIDENT (2026-07-26..29). The cutoff gated only the TICK branch of sweep()'s
candidate set, so a window whose receipt log existed but whose tick log had not yet
been written was swept immediately: _roots() computed tick_root over an EMPTY tick
list, _publish() skipped the absent tick file, _commit() anchored tick_n=0 on chain,
and _mark() retired it forever. 5 of 2812 published windows ended up with
anchor.json + receipts.jsonl and NO tick file, every one anchored `ticks=0` while
the recorder held 850-3201 real ticks. Because LF grading reads its prices from that
feed, ONE such window stalled every LF call spanning it for the full 6h abandon
deadline and then force-washed them.

The margin must gate BOTH branches. These tests fail on the pre-fix code.
"""
import json
import time

import importlib.util
import pathlib

import pytest

# neurons/ is deliberately not a package — the repo runs its scripts in place via
# sys.path (pyproject: "run in place ... not built or published as a wheel"), so
# load the module by path rather than adding an __init__.py just for the test.
_p = pathlib.Path(__file__).resolve().parent.parent / "neurons" / "hf_anchor.py"
_spec = importlib.util.spec_from_file_location("hf_anchor", _p)
A = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(A)


def _receipt_line(hk="5Hp3N8", seq=1, t0=0):
    return json.dumps({"receipt": {"hk": hk, "seq": seq, "ph": "ab" * 16,
                                   "t_recv_us": t0 * 1000, "grid_t0_ms": t0,
                                   "ing": "5FTc1V"}})


def _tick_line(a="BTCUSD", t=0, p=64000.0):
    return json.dumps({"a": a, "b": p - 1, "k": p + 1, "p": p, "t": t})


@pytest.fixture
def env(tmp_path, monkeypatch):
    log, tick, pub = tmp_path / "log", tmp_path / "ticks", tmp_path / "pub"
    for d in (log, tick, pub):
        d.mkdir()
    monkeypatch.setattr(A, "LOG_DIR", log)
    monkeypatch.setattr(A, "TICK_DIR", tick)
    monkeypatch.setattr(A, "STATE", tmp_path / "anchored.txt")
    monkeypatch.setattr(A, "PUBLIC_DIR", str(pub))
    return log, tick, pub


def _anchorer():
    a = A.Anchorer()
    a.wallet = None          # preview posture: publish, never commit
    a.sub = None
    return a


def _win(ago_windows: int) -> int:
    """A window id `ago_windows` windows behind now, aligned to the grid."""
    now = int(time.time() * 1000)
    return (now // A.WINDOW_MS - ago_windows) * A.WINDOW_MS


def test_receipt_bearing_window_inside_settle_margin_is_not_swept(env):
    """THE REGRESSION: receipts land before ticks; the window must be left alone."""
    log, tick, pub = env
    w = _win(0)                                    # current window — well inside the margin
    (log / f"{w}.jsonl").write_text(_receipt_line(t0=w) + "\n")
    # tick log deliberately absent — the recorder has not sealed it yet

    assert _anchorer().sweep() == 0, "swept a window still inside the settle margin"
    assert not (pub / str(w)).exists(), "published a window whose tick log had not sealed"
    assert w not in {int(x) for x in A._done()}, "retired the window forever"


def test_window_publishes_with_real_ticks_once_both_logs_seal(env):
    """Past the margin, with both logs present, everything lands — including ticks."""
    log, tick, pub = env
    w = _win(A.TICK_SETTLE_WINDOWS + 2)            # comfortably past the cutoff
    (log / f"{w}.jsonl").write_text(_receipt_line(t0=w) + "\n")
    (tick / f"{w}.ticks.jsonl").write_text(
        "\n".join(_tick_line(t=w + i, p=64000.0 + i) for i in range(5)) + "\n")

    assert _anchorer().sweep() == 1
    d = pub / str(w)
    assert (d / "receipts.jsonl").exists()
    assert (d / "ticks.jsonl").exists(), "published a receipt-bearing window with no ticks"
    assert len((d / "ticks.jsonl").read_text().strip().splitlines()) == 5


def test_ticks_arriving_late_are_still_published(env):
    """The exact incident sequence: sweep runs between the two seals, then the tick
    log arrives. The window must end up published WITH its ticks, never retired
    tick-less."""
    log, tick, pub = env
    w = _win(0)
    (log / f"{w}.jsonl").write_text(_receipt_line(t0=w) + "\n")

    a = _anchorer()
    assert a.sweep() == 0                          # held back by the margin

    # recorder seals the tick log a few seconds later, and the window ages out
    (tick / f"{w}.ticks.jsonl").write_text(
        "\n".join(_tick_line(t=w + i) for i in range(3)) + "\n")
    now = int(time.time() * 1000) + (A.TICK_SETTLE_WINDOWS + 2) * A.WINDOW_MS
    a2 = _anchorer()
    import unittest.mock as m
    with m.patch.object(A.time, "time", lambda: now / 1000.0):
        assert a2.sweep() == 1

    assert (pub / str(w) / "ticks.jsonl").exists(), "ticks stranded — the incident"
    assert len((pub / str(w) / "ticks.jsonl").read_text().strip().splitlines()) == 3


def test_roots_bind_the_real_ticks_not_an_empty_set(env):
    """tick_root must be computed over the sealed ticks. Anchoring tick_n=0 while
    real ticks exist is what permanently mis-bound the 5 incident windows."""
    log, tick, pub = env
    w = _win(A.TICK_SETTLE_WINDOWS + 2)
    (log / f"{w}.jsonl").write_text(_receipt_line(t0=w) + "\n")
    ticks = [json.loads(_tick_line(t=w + i, p=64000.0 + i)) for i in range(4)]
    (tick / f"{w}.ticks.jsonl").write_text(
        "\n".join(json.dumps(t) for t in ticks) + "\n")

    from sn89_signals import hf
    r = _anchorer()._roots(w)
    assert r["tick_n"] == 4
    assert r["tick_root"] == hf.tick_root(ticks)
    assert r["tick_root"] != hf.tick_root([])
