"""Protocol unit tests — schema, crypto round-trips, grader semantics,
validity filters, weights. Run on a box with the venv deps:

    pytest tests/ -x -q
"""
from __future__ import annotations

import time

import pytest

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sn89_signals import bucket, config, crypto, polygon, scoring
from sn89_signals.grader import LOST, PENDING, WASHED, WON, grade
from sn89_signals.schema import Signal, ValidationError, validate

HK = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
BANDS = {
    "BTCUSD": {"asset_class": "crypto", "tp_bps": 150, "sl_bps": 150},
    "XAUUSD": {"asset_class": "forex-commodities", "tp_bps": 50, "sl_bps": 50},
}


def _sig(pair="BTCUSD", direction="LONG", **kw) -> Signal:
    band = BANDS[pair]
    d = dict(trade_pair=pair, direction=direction, tp_bps=band["tp_bps"],
             sl_bps=band["sl_bps"], ts_miner=int(time.time() * 1000), hotkey=HK,
             asset_class=band["asset_class"])
    d.update(kw)
    return Signal(**d)


# ── schema ────────────────────────────────────────────────────────────────────
class TestSchema:
    def test_roundtrip_canonical(self):
        s = _sig()
        assert Signal.from_bytes(s.canonical_bytes()) == s

    def test_commitment_hiding(self):
        a, b = _sig(), _sig()  # identical setup, different nonce
        assert a.commitment() != b.commitment()

    def test_validate_ok(self):
        validate(_sig(), BANDS)

    def test_validate_rejects_off_board(self):
        with pytest.raises(ValidationError):
            validate(_sig().__class__(**{**_sig().__dict__, "trade_pair": "DOGEUSD"}), BANDS)

    def test_validate_rejects_wrong_band(self):
        s = _sig()
        s.tp_bps = 999
        with pytest.raises(ValidationError):
            validate(s, BANDS)

    def test_validate_rejects_bad_direction(self):
        s = _sig()
        s.direction = "UP"
        with pytest.raises(ValidationError):
            validate(s, BANDS)


# ── crypto ────────────────────────────────────────────────────────────────────
class TestCrypto:
    def test_owner_path_roundtrip(self):
        sk, pk = crypto.owner_keypair()
        s = _sig()
        pt = s.canonical_bytes()
        rnd = crypto.target_round(time.time() + 3600)
        blob = crypto.encrypt(pt, HK, rnd, pk)
        assert crypto.decrypt_owner(blob, sk) == pt

    def test_owner_path_wrong_key_fails(self):
        sk1, pk1 = crypto.owner_keypair()
        sk2, _ = crypto.owner_keypair()
        blob = crypto.encrypt(b"x" * 100, HK, 123456, pk1)
        assert crypto.decrypt_owner(blob, sk2) is None

    def test_binding_blocks_hotkey_swap(self):
        sk, pk = crypto.owner_keypair()
        blob = crypto.encrypt(b"y" * 50, HK, 123456, pk)
        blob["hk"] = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
        assert crypto.decrypt_owner(blob, sk) is None

    def test_commit_mismatch_rejected(self):
        sk, pk = crypto.owner_keypair()
        blob = crypto.encrypt(b"z" * 50, HK, 123456, pk)
        blob["commit"] = "00" * 32
        assert crypto.decrypt_owner(blob, sk) is None

    def test_timelock_roundtrip_past_round(self):
        """Use an already-matured drand round so tld works immediately
        (network call to drand API)."""
        sk, pk = crypto.owner_keypair()
        rnd = crypto.target_round(time.time() - 60)  # already matured
        sig = crypto.fetch_drand_signature(rnd)
        if sig is None:
            pytest.skip("drand API unreachable")
        pt = _sig().canonical_bytes()
        blob = crypto.encrypt(pt, HK, rnd, pk)
        assert crypto.decrypt_timelock(blob, sig) == pt

    def test_round_window(self):
        t0 = time.time()
        good = crypto.target_round(t0 + config.REVEAL_DELAY_S)
        bad = crypto.target_round(t0 + config.REVEAL_DELAY_S + 3600)
        assert crypto.expected_round_ok(good, t0)
        assert not crypto.expected_round_ok(bad, t0)


# ── grader semantics ───────────────────────────────────────────────────────────────────────────────
def _bars(*rows):
    return [{"t": t, "o": o, "h": h, "l": l, "c": c} for (t, o, h, l, c) in rows]


class TestGrader:
    T0 = 1_700_000_000_000
    ENTRY = 100.0

    def _grade(self, direction, bars, now_off_h=1, horizon=72):
        s = _sig(direction=direction, horizon_h=horizon)
        return grade(s, self.T0, self.T0 + now_off_h * 3_600_000,
                     entry_price=self.ENTRY, bars=bars)

    def test_long_tp_touch_wins(self):
        # BTC band 150bps → tp 101.5 / sl 98.5
        g = self._grade("LONG", _bars((self.T0 + 60_000, 100, 101.6, 99.9, 101)))
        assert (g.status, g.outcome_bps, g.exit_reason) == (WON, 150, "tp_touch")

    def test_long_sl_touch_loses(self):
        g = self._grade("LONG", _bars((self.T0 + 60_000, 100, 100.5, 98.4, 99)))
        assert (g.status, g.outcome_bps, g.exit_reason) == (LOST, -150, "sl_touch")

    def test_both_touched_same_candle_is_lost(self):
        """Single candle pierces both levels ⇒ SL first (conservative)."""
        g = self._grade("LONG", _bars((self.T0 + 60_000, 100, 101.6, 98.4, 100)))
        assert g.status == LOST

    def test_first_touch_wins_ordering(self):
        g = self._grade("LONG", _bars(
            (self.T0 + 60_000, 100, 101.6, 99.9, 101.5),   # TP touch first bar
            (self.T0 + 120_000, 101.5, 101.6, 98.0, 98.2),  # SL later — too late
        ))
        assert g.status == WON

    def test_short_tp_touch(self):
        # SHORT: tp below entry (98.5), sl above (101.5)
        g = self._grade("SHORT", _bars((self.T0 + 60_000, 100, 100.2, 98.4, 99)))
        assert (g.status, g.outcome_bps) == (WON, 150)

    def test_no_touch_within_horizon_pending(self):
        g = self._grade("LONG", _bars((self.T0 + 60_000, 100, 100.5, 99.5, 100)))
        assert g.status == PENDING

    def test_horizon_expiry_washes(self):
        g = self._grade("LONG", _bars((self.T0 + 60_000, 100, 100.5, 99.5, 100.2)),
                        now_off_h=80)
        assert (g.status, g.exit_reason) == (WASHED, "horizon_expired")

    def test_pre_t0_bars_ignored(self):
        g = self._grade("LONG", _bars((self.T0 - 60_000, 100, 105, 95, 100)))
        assert g.status == PENDING

    def test_wick_touch_counts(self):
        """Submission #78 lesson: the wick IS the fill."""
        g = self._grade("LONG", _bars((self.T0 + 60_000, 100, 101.5, 100, 100.1)))
        assert g.status == WON  # high exactly at tp → touched


# ── bad-tick sanitization (HYPE 2026-06-06 lesson) ───────────────────────────
class TestSanitize:
    """A lone off-market print must not trigger TP or SL; a real move
    corroborated by the second feed (Hyperliquid) must stand."""
    T = 1_700_000_000_000
    # body ~58, one +6.2% rogue wick on the middle candle (the real incident)
    SPIKE = _bars((T,           58.00, 58.10, 57.90, 58.05),
                  (T + 60_000,  58.05, 61.67, 58.00, 58.02),   # rogue high
                  (T + 120_000, 58.02, 58.12, 57.95, 58.08))

    def test_uncorroborated_spike_clamped(self):
        out = polygon.sanitize_minute_bars("HYPEUSD", "crypto", self.SPIKE,
                                           hl_fetch=lambda *a: None)
        assert out[1]["h"] == pytest.approx(58.12)  # clamped to ref (neighbours' max)
        assert out[1]["l"] == self.SPIKE[1]["l"]    # untouched side preserved

    def test_hl_corroborated_spike_stands(self):
        hl = {self.T + 60_000: {"h": 61.70, "l": 57.99}}
        out = polygon.sanitize_minute_bars("HYPEUSD", "crypto", self.SPIKE,
                                           hl_fetch=lambda *a: hl)
        assert out[1]["h"] == 61.67                 # HL reached it — real move

    def test_non_crypto_never_appeals(self):
        called = []
        out = polygon.sanitize_minute_bars("XAUUSD", "forex-commodities", self.SPIKE,
                                           hl_fetch=lambda *a: called.append(1))
        assert not called and out[1]["h"] == pytest.approx(58.12)

    def test_normal_wicks_untouched(self):
        bars = _bars((self.T, 100, 100.5, 99.6, 100.2),
                     (self.T + 60_000, 100.2, 100.9, 99.9, 100.6),
                     (self.T + 120_000, 100.6, 101.0, 100.1, 100.8))
        assert polygon.sanitize_minute_bars("BTCUSD", "crypto", bars,
                                            hl_fetch=lambda *a: None) == bars

    def test_spike_no_longer_falsely_stops_short(self):
        """End-to-end: the rogue wick would SL a short pre-filter; post-filter
        the trade keeps running (and here goes on to win)."""
        s = _sig(pair="BTCUSD", direction="SHORT")        # 150bps band on 58.0
        entry = 58.0                                       # sl 58.87 / tp 57.13
        dirty = _bars((self.T,           58.00, 58.10, 57.90, 58.05),
                      (self.T + 60_000,  58.05, 61.67, 58.00, 58.02),  # rogue
                      (self.T + 120_000, 58.02, 58.12, 57.10, 57.20))  # real TP
        g_dirty = grade(s, self.T, self.T + 3_600_000, entry_price=entry, bars=dirty)
        assert g_dirty.status == LOST                      # the bug
        clean = polygon.sanitize_minute_bars("BTCUSD", "crypto", dirty,
                                             hl_fetch=lambda *a: None)
        g_clean = grade(s, self.T, self.T + 3_600_000, entry_price=entry, bars=clean)
        assert (g_clean.status, g_clean.exit_reason) == (WON, "tp_touch")


# ── blob relay transport ─────────────────────────────────────────────────────
class TestRelay:
    BLOB = {"C": "deadbeef", "W_time": "x", "W_owner": "y"}

    def test_relay_upload_posts_and_returns_url(self, monkeypatch):
        sent = {}

        class _Resp:
            status_code = 200
            def json(self):
                return {"ok": True,
                        "url": "https://relay.example/sn89/blob/5HK/n1.json"}

        def _post(url, headers=None, json=None, timeout=None):
            sent.update(url=url, headers=headers, body=json)
            return _Resp()

        monkeypatch.setattr(bucket.requests, "post", _post)
        monkeypatch.setattr(config, "RELAY_TOKEN", "tok123")
        monkeypatch.setattr(config, "RELAY_URL", "https://relay.example/api/sn89/blob")
        url = bucket._relay_upload(self.BLOB, "5HK", "n1")
        assert url == "https://relay.example/sn89/blob/5HK/n1.json"
        assert sent["headers"]["Authorization"] == "Bearer tok123"
        assert sent["body"] == {"hotkey": "5HK", "nonce": "n1", "blob": self.BLOB}

    def test_upload_prefers_relay_when_no_bucket_creds(self, monkeypatch):
        monkeypatch.setattr(bucket, "BLOB_DIR", "")
        monkeypatch.setattr(config, "R2_ACCESS_KEY_ID", "")
        monkeypatch.setattr(config, "RELAY_TOKEN", "tok")
        called = {}
        def _fake(b, h, n):
            called["hit"] = (h, n)
            return "u"
        monkeypatch.setattr(bucket, "_relay_upload", _fake)
        assert bucket.upload(self.BLOB, "5HK", "n2") == "u"
        assert called["hit"] == ("5HK", "n2")

    def test_update_index_noop_in_relay_mode(self, monkeypatch):
        monkeypatch.setattr(config, "R2_ACCESS_KEY_ID", "")
        monkeypatch.setattr(config, "RELAY_TOKEN", "tok")
        # would raise if it tried boto3/disk; relay mode returns immediately
        assert bucket.update_index("5HK", "n3") is None


# ── validity filters ─────────────────────────────────────────────────────────
def _row(hk, pair, direction, t0):
    return scoring.GradedRow(hotkey=hk, trade_pair=pair, direction=direction,
                             t0_unix=t0, status="ok")


class TestValidity:
    def test_plagiarism_cooldown_voids_second(self):
        rows = scoring.apply_validity_filters([
            _row("A", "BTCUSD", "LONG", 1000),
            _row("B", "BTCUSD", "LONG", 1000 + 600),       # 10 min later — inside 15m
        ])
        assert rows[0].status == "ok"
        assert (rows[1].status, rows[1].void_reason) == ("void", "plagiarism_cooldown")

    def test_cooldown_expires(self):
        rows = scoring.apply_validity_filters([
            _row("A", "BTCUSD", "LONG", 1000),
            _row("B", "BTCUSD", "LONG", 1000 + 901),        # 15m+1s — clean
        ])
        assert rows[1].status == "ok"

    def test_opposite_direction_not_blocked(self):
        rows = scoring.apply_validity_filters([
            _row("A", "BTCUSD", "LONG", 1000),
            _row("B", "BTCUSD", "SHORT", 1000 + 60),
        ])
        assert rows[1].status == "ok"

    def test_first_commit_wins_ordering(self):
        rows = scoring.apply_validity_filters([
            _row("B", "BTCUSD", "LONG", 1000 + 60),
            _row("A", "BTCUSD", "LONG", 1000),              # earlier — wins despite list order
        ])
        byhk = {r.hotkey: r for r in rows}
        assert byhk["A"].status == "ok"
        assert byhk["B"].status == "void"

    def test_min_spacing(self):
        rows = scoring.apply_validity_filters([
            _row("A", "BTCUSD", "LONG", 0),
            _row("A", "XAUUSD", "LONG", 3600),               # 1h later < 4h
        ])
        assert (rows[1].status, rows[1].void_reason) == ("void", "min_spacing")

    def test_daily_quota(self):
        rows = scoring.apply_validity_filters([
            _row("A", "BTCUSD", "LONG", 0),
            _row("A", "XAUUSD", "LONG", 4 * 3600 + 1),
            _row("A", "BTCUSD", "SHORT", 8 * 3600 + 2),
            _row("A", "XAUUSD", "SHORT", 12 * 3600 + 3),     # 4th same UTC day
        ])
        assert (rows[3].status, rows[3].void_reason) == ("void", "daily_quota")

    def test_voided_doesnt_start_cooldown(self):
        rows = scoring.apply_validity_filters([
            _row("A", "BTCUSD", "LONG", 1000),
            _row("B", "BTCUSD", "LONG", 1000 + 60),          # void (cooldown from A)
            _row("C", "BTCUSD", "LONG", 1000 + 901),         # past A's window — ok
        ])
        assert [r.status for r in rows] == ["ok", "void", "ok"]


# ── weights ──────────────────────────────────────────────────────────────────
def _state(uid, first_seen, lifetime, wins, decisive, now):
    return scoring.MinerState(hotkey=f"hk{uid}", uid=uid, first_seen_unix=first_seen,
                              lifetime_decisive=lifetime, trailing_wins=wins,
                              trailing_decisive=decisive)


class TestWeights:
    NOW = 10_000_000.0
    OLD = NOW - config.IMMUNITY_S - 1

    def test_pro_rata_among_qualified(self):
        w = scoring.compute_weights([
            _state(1, self.OLD, 30, 6, 10, self.NOW),    # 60% hit, 6 wins
            _state(2, self.OLD, 25, 3, 5, self.NOW),     # 60% hit, 3 wins
        ], self.NOW)
        assert abs(w[1] / w[2] - 2.0) < 1e-9             # 6:3 pro-rata

    def test_coinflipper_gets_zero(self):
        w = scoring.compute_weights([
            _state(1, self.OLD, 40, 10, 20, self.NOW),   # 50% — below gate
            _state(2, self.OLD, 30, 6, 10, self.NOW),    # qualified
        ], self.NOW)
        assert 1 not in w
        assert w[2] > 0.99

    def test_under_20_lifetime_not_qualified(self):
        w = scoring.compute_weights([
            _state(1, self.OLD, 19, 8, 10, self.NOW),    # 80% but only 19 decisive
        ], self.NOW)
        assert 1 not in w
        assert w[config.BURN_UID] > 0.99

    def test_immunity_dust(self):
        w = scoring.compute_weights([
            _state(1, self.NOW - 86_400, 0, 0, 0, self.NOW),  # day-old miner
            _state(2, self.OLD, 30, 6, 10, self.NOW),
        ], self.NOW)
        assert w[1] == pytest.approx(config.DUST_WEIGHT, rel=1e-6)

    def test_nobody_qualified_burns(self):
        w = scoring.compute_weights([
            _state(1, self.OLD, 5, 2, 4, self.NOW),
        ], self.NOW)
        assert w == {config.BURN_UID: 1.0}

    def test_weights_normalized(self):
        w = scoring.compute_weights([
            _state(1, self.NOW - 100, 0, 0, 0, self.NOW),
            _state(2, self.OLD, 30, 6, 10, self.NOW),
            _state(3, self.OLD, 22, 11, 20, self.NOW),
        ], self.NOW)
        assert sum(w.values()) == pytest.approx(1.0)
