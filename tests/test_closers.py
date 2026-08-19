"""Closers competition — consensus unit tests for scoring, rate rules and the
ingest-side validation. Every property here traces to a measured exploit in the
2026-07-31 design review; a relaxation that fails one of these is reopening a
hole, not simplifying code."""
import pytest

from sn89_signals import closers

DAY_MS = 86_400_000

POSITIONS = {
    "p1": {"id": "p1", "trade_pair": "BTCUSD", "direction": "LONG"},
    "p2": {"id": "p2", "trade_pair": "EURUSD", "direction": "SHORT"},
}


def _payload(pid="p1", pair="BTCUSD", direction="LONG", action="CLOSE"):
    return {"kind": "closers", "position_id": pid, "trade_pair": pair,
            "direction": direction, "action": action, "asset_class": "crypto"}


class TestValidation:
    def test_valid_hold_and_close_pass(self):
        for action in ("HOLD", "CLOSE"):
            closers.validate_submission(_payload(action=action), 0, POSITIONS)

    def test_unknown_position_rejected(self):
        with pytest.raises(closers.ClosersRejected, match="unknown_position"):
            closers.validate_submission(_payload(pid="nope"), 0, POSITIONS)

    def test_pair_mismatch_rejected(self):
        # a miner cannot relabel our BTC long as an EUR position
        with pytest.raises(closers.ClosersRejected, match="pair_mismatch"):
            closers.validate_submission(_payload(pair="EURUSD"), 0, POSITIONS)

    def test_direction_mismatch_rejected(self):
        # lying about the position's direction flips the grade's sign — refuse it
        with pytest.raises(closers.ClosersRejected, match="direction_mismatch"):
            closers.validate_submission(_payload(direction="SHORT"), 0, POSITIONS)

    def test_bad_action_rejected(self):
        with pytest.raises(closers.ClosersRejected, match="bad_action"):
            closers.validate_submission(_payload(action="MAYBE"), 0, POSITIONS)


class TestRate:
    def test_daily_cap_binds(self):
        # the 30/day cap is the anti-variance-farming bound: without it a
        # zero-skill spammer's net-score sd grows √N and buys leaderboard rank
        gap = closers.CLOSERS_MIN_GAP_MS
        prior = [i * gap for i in range(closers.CLOSERS_MAX_PER_DAY)]
        with pytest.raises(closers.ClosersRejected, match="daily_cap"):
            closers.check_rate(prior, prior[-1] + gap)

    def test_min_gap_binds(self):
        with pytest.raises(closers.ClosersRejected, match="min_gap"):
            closers.check_rate([0], closers.CLOSERS_MIN_GAP_MS - 1)

    def test_next_utc_day_resets_the_cap(self):
        gap = closers.CLOSERS_MIN_GAP_MS
        prior = [i * gap for i in range(closers.CLOSERS_MAX_PER_DAY)]
        closers.check_rate(prior, DAY_MS + gap)      # no raise


class TestScore:
    def test_hold_on_favorable_move_is_positive(self):
        # LONG improves 1σ (35.3 bps on BTC): HOLD was right
        s = closers.call_score("LONG", "HOLD", 100000.0, 100353.0, "BTCUSD")
        assert s == pytest.approx(1.0, abs=0.01)

    def test_close_on_adverse_move_is_positive(self):
        # LONG deteriorates: CLOSE was right, same magnitude
        s = closers.call_score("LONG", "CLOSE", 100000.0, 99647.0, "BTCUSD")
        assert s == pytest.approx(1.0, abs=0.01)

    def test_hold_and_close_are_exact_mirrors(self):
        # symmetry is the anti-free-button property: no premium on either
        # action, so neither can be farmed blind (a flat CLOSE premium makes
        # spamming CLOSE +EV at 50/50 — the design discussion's trap)
        h = closers.call_score("LONG", "HOLD", 100.0, 101.0, "BTCUSD")
        c = closers.call_score("LONG", "CLOSE", 100.0, 101.0, "BTCUSD")
        assert h == pytest.approx(-c)

    def test_short_direction_flips_favorability(self):
        # price falls: a SHORT improved, so HOLD scores positive
        s = closers.call_score("SHORT", "HOLD", 100.0, 99.0, "BTCUSD")
        assert s > 0

    def test_vol_normalization_equalizes_assets(self):
        # identical σ-multiples score identically: 1σ on EURUSD (6.5 bps)
        # equals 1σ on BTC (35.3 bps). Without this the board ranks asset
        # selection — the 16× σ spread would drown exit judgment.
        eur = closers.call_score("LONG", "HOLD", 1.0, 1.0 + 6.5e-4, "EURUSD")
        btc = closers.call_score("LONG", "HOLD", 1.0, 1.0 + 35.3e-4, "BTCUSD")
        assert eur == pytest.approx(btc, rel=0.01)

    def test_winsorized_at_three_sigma(self):
        # tail clipping: a 10σ jackpot pays the same as 3σ, so best-of-N sybil
        # keys can't farm the fat tail of crypto hours
        s = closers.call_score("LONG", "HOLD", 100.0, 110.0, "BTCUSD")
        assert s == pytest.approx(closers.CLOSERS_WINSOR_Z)

    def test_unknown_pair_falls_back_by_class(self):
        assert closers.sigma_bps("NEWCOIN", "crypto") == \
            closers.SIGMA_FALLBACK_BPS["crypto"]


class TestWeightsShape:
    def test_min_calls_gate(self, tmp_path, monkeypatch):
        """A 2-lucky-call key ranks nowhere; a miner over the min-N with a
        positive sum earns; a negative-sum miner earns nothing."""
        import sqlite3, os
        cache = str(tmp_path)
        db = closers._db(cache)
        # t0 = now_ms, and the evaluation instant must therefore be at least one
        # CLOSERS_HORIZON_S later: a closers call resolves at t0 + horizon, and
        # grade() only writes a row once end_ms <= now, so a graded row whose
        # horizon has not elapsed cannot exist in production. These fixtures
        # used now+60s, which the point-in-time filter correctly rejects.
        now_ms = 1_800_000_000_000
        rows = []
        # hkA: 12 calls, +0.5 each → qualifies, positive
        rows += [(f"A:{i}", "hkA", now_ms, "BTCUSD", "CLOSE", 0.5, "graded")
                 for i in range(12)]
        # hkB: 2 calls, +3.0 each (lucky) → under min-N, no weight
        rows += [(f"B:{i}", "hkB", now_ms, "BTCUSD", "CLOSE", 3.0, "graded")
                 for i in range(2)]
        # hkC: 15 calls, −0.2 each → negative sum, no weight
        rows += [(f"C:{i}", "hkC", now_ms, "BTCUSD", "HOLD", -0.2, "graded")
                 for i in range(15)]
        db.executemany("INSERT INTO grades VALUES (?,?,?,?,?,?,?)", rows)
        db.commit(); db.close()
        monkeypatch.setattr(closers, "sync_and_grade", lambda *a, **k: None)
        monkeypatch.setattr(closers, "CLOSERS_REQUIRE_QUALIFIED", False)
        w = closers.closers_weights({"hkA": 1, "hkB": 2, "hkC": 3},
                                    now=now_ms / 1000 + closers.CLOSERS_HORIZON_S + 60, cache_dir=cache)
        assert w.get(1, 0) > 0
        assert w.get(2, 0) == 0
        assert w.get(3, 0) == 0
        assert sum(w.values()) == pytest.approx(1.0)

    def test_qualified_gate_excludes_unqualified(self, tmp_path, monkeypatch):
        cache = str(tmp_path)
        db = closers._db(cache)
        now_ms = 1_800_000_000_000
        db.executemany("INSERT INTO grades VALUES (?,?,?,?,?,?,?)",
                       [(f"A:{i}", "hkA", now_ms, "BTCUSD", "CLOSE", 0.5, "graded")
                        for i in range(12)])
        db.commit(); db.close()
        monkeypatch.setattr(closers, "sync_and_grade", lambda *a, **k: None)
        monkeypatch.setattr(closers, "CLOSERS_REQUIRE_QUALIFIED", True)
        w = closers.closers_weights({"hkA": 1}, now=now_ms / 1000 + closers.CLOSERS_HORIZON_S + 60,
                                    cache_dir=cache, qualified_hks=set())
        assert w.get(1, 0) == 0          # not HF/LF-qualified → nothing
        w2 = closers.closers_weights({"hkA": 1}, now=now_ms / 1000 + closers.CLOSERS_HORIZON_S + 60,
                                     cache_dir=cache, qualified_hks={"hkA"})
        assert w2.get(1, 0) > 0


class TestClosersDoesNotLockPairs:
    """A closers vote must never lock the pair on HF/LF.

    Closers receipts share the anchored windows with HF calls, and the LF void
    path rebuilds its lock index by walking those windows. Without a kind filter
    a HOLD/CLOSE vote voided the miner's own next LF call on that pair —
    Brian, 2026-08-04: closers USDJPY CLOSE at 03:42, LF USDJPY LONG voided at
    03:58 as pair_locked_other_mechanism.
    """

    def _window(self, tmp_path, entries):
        import json
        d = tmp_path / "1785000000000"
        d.mkdir(parents=True)
        (d / "receipts.jsonl").write_text("\n".join(json.dumps(e) for e in entries))
        (tmp_path / "index.json").write_text(json.dumps({"windows": [1785000000000]}))
        return tmp_path

    def _entry(self, kind, pair, ts):
        p = {"trade_pair": pair}
        if kind:
            p["kind"] = kind
        return {"submit": {"hk": "hk1", "payload": p},
                "receipt": {"grid_t0_ms": ts}}

    def test_closers_receipt_is_not_a_lock_row(self, tmp_path):
        from sn89_signals import hf_grade
        base = self._window(tmp_path, [self._entry("closers", "USDJPY", 1785000001000)])
        assert hf_grade.load_hf_lock_rows(f"file://{base}", 0) == []

    def test_hf_receipt_still_locks(self, tmp_path):
        from sn89_signals import hf_grade
        base = self._window(tmp_path, [self._entry("hf", "USDJPY", 1785000001000)])
        rows = hf_grade.load_hf_lock_rows(f"file://{base}", 0)
        assert [(r[0], r[1]) for r in rows] == [("hk1", "USDJPY")]
