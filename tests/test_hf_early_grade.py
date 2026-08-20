"""Early-decisive HF grading must be a LATENCY change, never a verdict change.

Canefis reported HF forex "pending" twice. It was not stuck: a call was graded
only after its full horizon plus a 900s settle, so a 2h forex call whose TP was
touched at minute 3 sat pending for 2h15m with the outcome already determined
(his seq 153: decided 12:03:40, written ~13:51).

The property under test is verdict-identity — the same call graded early and
graded at its horizon must produce the same status AND the same open_until_ms,
because open_until_ms feeds the same-pair open-position gate that voids
successors. A drift there would void legal calls.
"""
import sqlite3

import pytest

from sn89_signals import hf, hf_grade

HK = "5" + "H" * 47
PAIR = "EURUSD"                       # 2h horizon — the case that actually hurt
T0 = hf.HF_LAUNCH_FROM * 1000 + 3_600_000
HORIZON_S = 7200
END = T0 + HORIZON_S * 1000
SETTLE_MS = hf_grade.GRADE_SETTLE_S * 1000
# What the EARLY probe may read up to. Deliberately a different constant from
# SETTLE_MS: the horizon pass waits out GRADE_SETTLE_S because an unsettled tail
# manufactures false washes, while a decisive read only needs the tick to be
# FROZEN — which happens once its window seals, at worst ANCHOR_WINDOW_S plus the
# recorder's seal grace after the tick's own timestamp.
EARLY_MS = hf_grade.EARLY_SETTLE_S * 1000


def _entry_px():
    return 1.1000


def _ticks(touch_at_ms=None, up=True):
    """A flat series at entry, optionally with a decisive burst at touch_at_ms.

    The burst is MIN_TOUCH_TICKS long because a lone reverting tick never scores.
    """
    px = _entry_px()
    out = [{"a": PAIR, "t": T0 - 1000, "p": px, "b": px, "k": px}]
    t = T0 + 1000
    while t < END:
        p = px
        if touch_at_ms is not None and touch_at_ms <= t < touch_at_ms + 5000:
            p = px * (1 + (0.0010 if up else -0.0010))   # 10 bps, band is 5
        out.append({"a": PAIR, "t": t, "p": p, "b": p, "k": p})
        t += 1000
    return out


@pytest.fixture()
def cache(tmp_path):
    return str(tmp_path)


def _seed_pending(cache, key="{}:1".format(HK)):
    db = hf_grade._db(cache)
    db.execute("INSERT OR REPLACE INTO pending VALUES (?,?,?,?,?,?)",
               (key, HK, T0, PAIR, "LONG", END))
    db.commit()
    db.close()
    return key


def _run(monkeypatch, cache, ticks, now_ms):
    monkeypatch.setattr(hf_grade, "_index", lambda base: [])
    monkeypatch.setattr(hf_grade, "_ticks_for",
                        lambda base, d, pair, t0, end: (
                            [t for t in ticks if int(t["t"]) <= end], []))
    hf_grade.sync_and_grade("file:///nonexistent", cache, now_ms / 1000.0)
    db = sqlite3.connect(f"{cache}/hf_grades.db")
    rows = db.execute("SELECT key,status,open_until_ms FROM grades").fetchall()
    pend = db.execute("SELECT count(*) FROM pending").fetchone()[0]
    db.close()
    return rows, pend


class TestEarlyDecisive:
    def test_tp_touched_early_grades_before_the_horizon(self, monkeypatch, cache):
        """THE FIX. Touch at t0+3min; at t0+18min the settled series decides it."""
        key = _seed_pending(cache)
        touch = T0 + 180_000
        now = touch + SETTLE_MS + 60_000            # settled past the touch
        assert now < END, "must still be inside the horizon or this proves nothing"
        rows, pend = _run(monkeypatch, cache, _ticks(touch_at_ms=touch), now)
        assert rows and rows[0][0] == key
        assert rows[0][1] == "won", rows
        assert pend == 0

    def test_early_verdict_equals_horizon_verdict(self, monkeypatch, cache, tmp_path):
        """Status AND open_until_ms must match, since open_until_ms gates successors."""
        touch = T0 + 180_000
        ticks = _ticks(touch_at_ms=touch)

        _seed_pending(cache)
        early, _ = _run(monkeypatch, cache, ticks, touch + SETTLE_MS + 60_000)

        late_dir = str(tmp_path / "late")
        _seed_pending(late_dir)
        late, _ = _run(monkeypatch, cache=late_dir, ticks=ticks,
                       now_ms=END + SETTLE_MS + 1000)

        assert early == late, f"early {early} != horizon {late}"

    def test_untouched_call_is_not_graded_early(self, monkeypatch, cache):
        """A truncated series that touched nothing means 'not yet', never 'wash'."""
        _seed_pending(cache)
        rows, pend = _run(monkeypatch, cache, _ticks(), T0 + SETTLE_MS + 600_000)
        assert rows == []
        assert pend == 1

    def test_untouched_call_still_washes_at_the_horizon(self, monkeypatch, cache):
        _seed_pending(cache)
        rows, pend = _run(monkeypatch, cache, _ticks(), END + SETTLE_MS + 1000)
        assert [r[1] for r in rows] == ["wash"], rows
        assert pend == 0

    def test_a_touch_beyond_the_settle_point_is_not_read(self, monkeypatch, cache):
        """Only a FROZEN span may decide. A touch newer than the seal must wait.

        A tick still inside an unsealed window can be reordered by a late tick
        carrying an earlier src_ts, which on a marginal call is the difference
        between won and lost. So the probe must not read it yet.
        """
        _seed_pending(cache)
        touch = T0 + 3_600_000
        now = touch - 60_000 + EARLY_MS          # read bound is BEFORE the touch
        rows, pend = _run(monkeypatch, cache, _ticks(touch_at_ms=touch), now)
        assert rows == []
        assert pend == 1

    def test_a_touch_older_than_the_seal_is_read(self, monkeypatch, cache):
        """The complement, and the reason the bound is one window and not 900s.

        This is the case a real trader hit on 2026-08-20: an AUDUSD SHORT decided
        at 16:29:53 that the board did not show until ~16:50, because the probe
        was refusing to read anything newer than 15 minutes. Once the deciding
        tick's window is sealed there is nothing left to wait for.
        """
        _seed_pending(cache)
        touch = T0 + 3_600_000
        now = touch + 60_000 + EARLY_MS          # read bound is AFTER the touch
        rows, pend = _run(monkeypatch, cache, _ticks(touch_at_ms=touch), now)
        assert [r[1] for r in rows] == ["won"], rows
        assert pend == 0

    def test_a_tick_gap_blocks_an_early_grade(self, monkeypatch, cache):
        """A hole could hide the FIRST touch, which is the one that decides."""
        _seed_pending(cache)
        touch = T0 + 180_000
        ticks = _ticks(touch_at_ms=touch)
        monkeypatch.setattr(hf_grade, "_index", lambda base: [])
        monkeypatch.setattr(hf_grade, "_ticks_for",
                            lambda base, d, pair, t0, end: (
                                [t for t in ticks if int(t["t"]) <= end], [12345]))
        hf_grade.sync_and_grade("file:///nonexistent", cache,
                                (touch + SETTLE_MS + 60_000) / 1000.0)
        db = sqlite3.connect(f"{cache}/hf_grades.db")
        assert db.execute("SELECT count(*) FROM grades").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM pending").fetchone()[0] == 1
        db.close()

    def test_flag_off_restores_horizon_only_grading(self, monkeypatch, cache):
        monkeypatch.setattr(hf_grade, "EARLY_DECISIVE", False)
        _seed_pending(cache)
        touch = T0 + 180_000
        rows, pend = _run(monkeypatch, cache, _ticks(touch_at_ms=touch),
                          touch + SETTLE_MS + 60_000)
        assert rows == []
        assert pend == 1
