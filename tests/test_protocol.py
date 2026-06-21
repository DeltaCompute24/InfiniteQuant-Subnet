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

    def test_crypto_wash_window_is_30h(self):
        # class-fixed: crypto develops over 30h regardless of submitted horizon
        bars = _bars((self.T0 + 60_000, 100, 100.5, 99.5, 100.2))   # no touch (BTC 150bps)
        s = _sig(pair="BTCUSD", direction="LONG")
        assert grade(s, self.T0, self.T0 + 29 * 3_600_000,
                     entry_price=self.ENTRY, bars=bars).status == PENDING
        assert grade(s, self.T0, self.T0 + 31 * 3_600_000,
                     entry_price=self.ENTRY, bars=bars).status == WASHED

    def test_metals_wash_window_is_12h(self):
        # fx/metals wash sooner than crypto (12h vs 30h)
        bars = _bars((self.T0 + 60_000, 100, 100.4, 99.6, 100.1))   # no touch (XAU 50bps)
        s = _sig(pair="XAUUSD", direction="LONG")
        assert grade(s, self.T0, self.T0 + 11 * 3_600_000,
                     entry_price=self.ENTRY, bars=bars).status == PENDING
        assert grade(s, self.T0, self.T0 + 13 * 3_600_000,
                     entry_price=self.ENTRY, bars=bars).status == WASHED


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

    def test_noncrypto_wick_tol_is_tighter(self):
        # a +0.5% wick passes the crypto 1% gate but trips the non-crypto 0.25% gate
        bars = _bars((self.T,           100.0, 100.05, 99.95, 100.0),
                     (self.T + 60_000,  100.0, 100.50, 99.95, 100.0),   # +0.5% high
                     (self.T + 120_000, 100.0, 100.05, 99.95, 100.0))
        cy = polygon.sanitize_minute_bars("BTCUSD", "crypto", bars, hl_fetch=lambda *a: None)
        assert cy[1]["h"] == pytest.approx(100.50)        # crypto: kept
        fx = polygon.sanitize_minute_bars("EURUSD", "forex", bars, hl_fetch=lambda *a: None)
        assert fx[1]["h"] == pytest.approx(100.05)        # forex: clamped to ref

    def test_forex_rollover_bars_dropped(self):
        import datetime as _dt
        ms = lambda h, m: int(_dt.datetime(2024, 1, 2, h, m, tzinfo=_dt.timezone.utc)
                              .timestamp() * 1000)
        bars = _bars((ms(20, 54), 1.10, 1.101, 1.099, 1.100),
                     (ms(21, 0),  1.10, 1.500, 1.099, 1.100),   # rollover junk wick
                     (ms(21, 25), 1.10, 1.101, 1.099, 1.100))
        fx = polygon.sanitize_minute_bars("EURUSD", "forex", bars, hl_fetch=lambda *a: None)
        assert [b["t"] for b in fx] == [ms(20, 54), ms(21, 25)]   # 21:00 bar dropped
        cy = polygon.sanitize_minute_bars("BTCUSD", "crypto", bars, hl_fetch=lambda *a: None)
        assert len(cy) == 3                                       # crypto: 24/7, nothing dropped


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
    def test_same_pair_dir_across_miners_allowed(self):
        # the 15-min cross-miner cooldown was retired — two miners may hold the
        # same trade; shadowing is handled by detect_copiers, not by voiding.
        rows = scoring.apply_validity_filters([
            _row("A", "BTCUSD", "LONG", 1000),
            _row("B", "BTCUSD", "LONG", 1000 + 60),        # 1 min later — still ok
        ])
        assert [r.status for r in rows] == ["ok", "ok"]

    def test_opposite_direction_not_blocked(self):
        rows = scoring.apply_validity_filters([
            _row("A", "BTCUSD", "LONG", 1000),
            _row("B", "BTCUSD", "SHORT", 1000 + 60),
        ])
        assert rows[1].status == "ok"

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


# ── copy penalty: mark the later entrant on a live identical trade (§7.5) ─────
class TestMarkCopies:
    H = 72  # horizon hours; window = t0 → t0 + 72h

    def test_original_not_marked_copier_marked(self):
        rows = scoring.mark_copies([
            _row("A", "BTCUSD", "LONG", 0),
            _row("B", "BTCUSD", "LONG", 3600),       # 1h later, A still open
        ])
        byhk = {r.hotkey: r for r in rows}
        assert byhk["A"].is_copy is False            # first mover safe
        assert byhk["B"].is_copy is True             # copied a live trade

    def test_first_mover_safe_regardless_of_list_order(self):
        rows = scoring.mark_copies([
            _row("B", "BTCUSD", "LONG", 3600),
            _row("A", "BTCUSD", "LONG", 0),           # earlier → original despite order
        ])
        byhk = {r.hotkey: r for r in rows}
        assert byhk["A"].is_copy is False
        assert byhk["B"].is_copy is True

    def test_no_overlap_not_a_copy(self):
        rows = scoring.mark_copies([
            _row("A", "BTCUSD", "LONG", 0),
            _row("B", "BTCUSD", "LONG", self.H * 3600 + 1),   # opens after A's horizon
        ])
        assert all(r.is_copy is False for r in rows)

    def test_opposite_direction_not_a_copy(self):
        rows = scoring.mark_copies([
            _row("A", "BTCUSD", "LONG", 0),
            _row("B", "BTCUSD", "SHORT", 3600),
        ])
        assert all(r.is_copy is False for r in rows)

    def test_same_hotkey_reentry_not_a_copy(self):
        rows = scoring.mark_copies([
            _row("A", "BTCUSD", "LONG", 0),
            _row("A", "BTCUSD", "LONG", 3600),        # same operator, not copying another
        ])
        assert all(r.is_copy is False for r in rows)

    def test_sybil_spray_all_but_first_marked(self):
        rows = scoring.mark_copies([
            _row(f"k{i}", "BTCUSD", "LONG", i * 60) for i in range(10)
        ])
        marked = [r.hotkey for r in rows if r.is_copy]
        assert "k0" not in marked                     # original banks the win
        assert len(marked) == 9                        # other 9 keys penalized


class TestHabitualCopier:
    def test_consistent_copier_is_habitual(self):
        # 9 of 12 decisive trades landed second → rotating copier caught
        assert scoring.is_habitual_copier(copies=9, decisive=12) is True

    def test_occasional_second_lander_protected(self):
        # an honest miner who lands second 3 of 20 times is NOT penalized
        assert scoring.is_habitual_copier(copies=3, decisive=20) is False

    def test_new_miner_below_min_copies_safe(self):
        # 2/2 looks like 100% but is below COPY_MIN_COPIES — not flagged
        assert scoring.is_habitual_copier(copies=2, decisive=2) is False

    def test_at_threshold_fires(self):
        assert scoring.is_habitual_copier(
            copies=config.COPY_MIN_COPIES,
            decisive=int(config.COPY_MIN_COPIES / config.COPY_HABITUAL_RATE)) is True

    def test_no_decisive_is_safe(self):
        assert scoring.is_habitual_copier(copies=0, decisive=0) is False


# ── copy / collusion forensics: 30-day shadowing report (§7.5) ────────────────
class TestCopyDetection:
    NOW = 100 * 24 * 3600.0

    def _follows(self, leader_t, follower_lag, n, pair="BTCUSD", direction="LONG",
                 leader="A", follower="B"):
        """n leader→follower pairs, spaced a day apart so per-day quota is moot."""
        rows = []
        for i in range(n):
            base = leader_t + i * 86_400
            rows.append(_row(leader, pair, direction, base))
            rows.append(_row(follower, pair, direction, base + follower_lag))
        return rows

    def test_sharp_copier_flagged(self):
        rows = self._follows(self.NOW - 20 * 86_400, 300, config.COPY_SHARP_MIN_EVENTS)
        reports = scoring.detect_copiers(rows, self.NOW)
        assert "B" in reports
        assert reports["B"][0].leader == "A"
        assert reports["B"][0].flagged is True
        assert scoring.flagged_copier_hotkeys(reports) == {"B"}

    def test_leader_not_flagged(self):
        rows = self._follows(self.NOW - 20 * 86_400, 300, config.COPY_SHARP_MIN_EVENTS)
        reports = scoring.detect_copiers(rows, self.NOW)
        assert "A" not in reports                       # first commit is innocent

    def test_below_threshold_not_flagged(self):
        rows = self._follows(self.NOW - 10 * 86_400, 300, config.COPY_SHARP_MIN_EVENTS - 1)
        reports = scoring.detect_copiers(rows, self.NOW)
        assert scoring.flagged_copier_hotkeys(reports) == set()

    def test_soft_shadowing_is_report_only(self):
        # within 24h but never within 15min: low_diversity, never flagged
        rows = self._follows(self.NOW - 29 * 86_400, 6 * 3600, config.COPY_SOFT_MIN_EVENTS)
        reports = scoring.detect_copiers(rows, self.NOW)
        rep = reports["B"][0]
        assert rep.low_diversity is True
        assert rep.flagged is False
        assert scoring.flagged_copier_hotkeys(reports) == set()

    def test_window_rolls_off(self):
        # all coincidences older than COPY_WINDOW_S ⇒ nothing flagged (reversible)
        rows = self._follows(self.NOW - config.COPY_WINDOW_S - 10 * 86_400, 300,
                             config.COPY_SHARP_MIN_EVENTS)
        reports = scoring.detect_copiers(rows, self.NOW)
        assert reports == {}

    def test_flagged_copier_zeroed_in_weights(self):
        states = [
            _state(1, self.NOW - config.IMMUNITY_S - 1, 18, 30, 6),  # leader (60%)
            _state(2, self.NOW - config.IMMUNITY_S - 1, 18, 30, 6),  # copier (identical)
        ]
        w_clean = scoring.compute_weights(states, self.NOW)
        assert w_clean[1] == pytest.approx(w_clean[2])      # equal pro-rata when clean
        w_flagged = scoring.compute_weights(states, self.NOW, excluded_uids={2})
        assert 2 not in w_flagged
        assert w_flagged[1] > 0.99                          # copier's share goes to leader/burn


# ── weights ──────────────────────────────────────────────────────────────────
# _state(uid, first_seen, lifetime_wins, lifetime_decisive, trailing_wins):
#   hit-rate/tier come from lifetime_wins / lifetime_decisive (forever);
#   the emission share is sized by trailing_wins (last 30 days).
def _state(uid, first_seen, lw, ld, tw):
    return scoring.MinerState(hotkey=f"hk{uid}", uid=uid, first_seen_unix=first_seen,
                              lifetime_wins=lw, lifetime_decisive=ld, trailing_wins=tw)


class TestWeights:
    NOW = 10_000_000.0
    OLD = NOW - config.IMMUNITY_S - 1

    def test_pro_rata_among_qualified(self):
        w = scoring.compute_weights([
            _state(1, self.OLD, 18, 30, 6),    # 60% lifetime (SHARP), 6 recent wins
            _state(2, self.OLD, 15, 25, 3),    # 60% lifetime (SHARP), 3 recent wins
        ], self.NOW)
        assert abs(w[1] / w[2] - 2.0) < 1e-9   # 6:3 on recent wins (same tier)

    def test_coinflipper_gets_zero(self):
        w = scoring.compute_weights([
            _state(1, self.OLD, 20, 40, 10),   # 50% lifetime — below gate
            _state(2, self.OLD, 18, 30, 6),    # 60% lifetime — qualified
        ], self.NOW)
        assert 1 not in w
        assert w[2] > 0.99

    def test_under_20_lifetime_not_qualified(self):
        w = scoring.compute_weights([
            _state(1, self.OLD, 16, 19, 8),    # 84% but only 19 lifetime decisive
        ], self.NOW)
        assert 1 not in w
        assert w[config.BURN_UID] > 0.99

    def test_qualified_but_idle_earns_zero(self):
        # career WOLF with zero recent wins earns nothing — must keep trading
        w = scoring.compute_weights([
            _state(1, self.OLD, 70, 100, 0),   # 70% lifetime, 0 recent wins
            _state(2, self.OLD, 18, 30, 6),    # 60% lifetime, 6 recent wins
        ], self.NOW)
        assert w.get(1, 0.0) == pytest.approx(0.0)
        assert w[2] > 0.99

    def test_immunity_dust(self):
        w = scoring.compute_weights([
            _state(1, self.NOW - 86_400, 0, 0, 0),   # day-old miner
            _state(2, self.OLD, 18, 30, 6),
        ], self.NOW)
        assert w[1] == pytest.approx(config.DUST_WEIGHT, rel=1e-6)

    def test_nobody_qualified_burns(self):
        w = scoring.compute_weights([
            _state(1, self.OLD, 2, 4, 2),      # 50%, 4 lifetime decisive
        ], self.NOW)
        assert w == {config.BURN_UID: 1.0}

    def test_weights_normalized(self):
        w = scoring.compute_weights([
            _state(1, self.NOW - 100, 0, 0, 0),
            _state(2, self.OLD, 18, 30, 6),
            _state(3, self.OLD, 12, 22, 11),
        ], self.NOW)
        assert sum(w.values()) == pytest.approx(1.0)


class TestWinRateTiers:
    NOW = 10_000_000.0
    OLD = NOW - config.IMMUNITY_S - 1

    def test_multiplier_thresholds(self):
        assert scoring.win_multiplier(0.52) == 0.0     # below gate
        assert scoring.win_multiplier(0.53) == 1.0     # QUALIFIED
        assert scoring.win_multiplier(0.59) == 1.0
        assert scoring.win_multiplier(0.60) == 1.2     # SHARP
        assert scoring.win_multiplier(0.69) == 1.2
        assert scoring.win_multiplier(0.70) == 2.0     # WOLF
        assert scoring.win_multiplier(0.95) == 2.0

    def test_wolf_doubles_qualified_at_equal_recent_wins(self):
        # equal recent wins (14), different LIFETIME tiers → WOLF earns 2× per win
        w = scoring.compute_weights([
            _state(1, self.OLD, 14, 20, 14),   # 70% lifetime WOLF  → eff 28
            _state(2, self.OLD, 14, 26, 14),   # 53.8% lifetime QUAL → eff 14
        ], self.NOW)
        assert w[1] / w[2] == pytest.approx(2.0)

    def test_sharp_beats_qualified_at_equal_recent_wins(self):
        w = scoring.compute_weights([
            _state(1, self.OLD, 12, 20, 12),   # 60% lifetime SHARP → eff 14.4
            _state(2, self.OLD, 12, 22, 12),   # 54.5% lifetime QUAL → eff 12
        ], self.NOW)
        assert w[1] / w[2] == pytest.approx(1.2)

    def test_53_gate_excludes_52(self):
        w = scoring.compute_weights([
            _state(1, self.OLD, 13, 25, 13),   # 52% lifetime — just below 53% gate
        ], self.NOW)
        assert 1 not in w
        assert w[config.BURN_UID] > 0.99


class TestCollateralGating:
    # verifies the collateral gate composes with the lifetime/excluded model
    NOW = 10_000_000.0
    OLD = NOW - config.IMMUNITY_S - 1

    def _s(self, uid, lw, ld, tw, rao):
        return scoring.MinerState(
            hotkey=f"hk{uid}", uid=uid, first_seen_unix=self.OLD,
            lifetime_wins=lw, lifetime_decisive=ld, trailing_wins=tw, collateral_rao=rao)

    def test_unfunded_held_to_dust(self):
        w = scoring.compute_weights(
            [self._s(1, 18, 30, 6, 1_000_000),    # funded, qualified
             self._s(2, 18, 30, 6, 0)],           # qualified by hit-rate but unfunded
            self.NOW, min_collateral_rao=500_000)
        assert w[1] > 0.99
        assert w[2] == pytest.approx(config.DUST_WEIGHT, rel=1e-6)

    def test_gating_off_ignores_funds(self):
        w = scoring.compute_weights(
            [self._s(1, 18, 30, 6, 0), self._s(2, 18, 30, 3, 0)],
            self.NOW, min_collateral_rao=0)
        assert abs(w[1] / w[2] - 2.0) < 1e-9       # pure 6:3 emission split

    def test_excluded_copier_gets_nothing_even_if_funded(self):
        w = scoring.compute_weights(
            [self._s(1, 18, 30, 6, 1_000_000), self._s(2, 18, 30, 6, 1_000_000)],
            self.NOW, excluded_uids={2}, min_collateral_rao=500_000)
        assert 2 not in w
        assert w[1] > 0.99


# ── forfeit on non-revelation (§6.4 — closes the selective-reveal option) ─────
class TestForfeitUnrevealed:
    """A committed signal whose blob is never served must grade LOST, so the
    24h timelock can't be used as a costless option (commit, watch the market,
    publish only winners). Exercises the real Validator.forfeit_unrevealed
    against an in-memory journal."""

    def _validator(self):
        import sqlite3
        import sys
        import types
        # validator.py imports bittensor at module top only for __init__/chain;
        # forfeit_unrevealed needs none of it. Stub the module so the real method
        # runs even on a box without the (heavy, no-macOS-wheel) bittensor dep.
        if "bittensor" not in sys.modules:
            sys.modules["bittensor"] = types.ModuleType("bittensor")
        from neurons.validator import SCHEMA, Validator
        v = Validator.__new__(Validator)            # skip __init__ (no chain/RPC)
        v.db = sqlite3.connect(":memory:")
        v.db.executescript(SCHEMA)
        return v

    def _seal(self, v, commit, *, blob=None, matured_ago_s):
        # round whose maturity is `matured_ago_s` in the past (negative ⇒ future)
        rnd = crypto.target_round(time.time() - matured_ago_s)
        v.db.execute(
            "INSERT INTO signals (commit_hex,hotkey,round,url_tag,"
            "first_seen_block,t0_unix,blob_json,status) VALUES (?,?,?,?,?,?,?,'sealed')",
            (commit, HK, rnd, "tag", 1, time.time() - matured_ago_s - 86_400, blob))
        v.db.commit()

    def _status(self, v, commit):
        return v.db.execute(
            "SELECT status, exit_reason FROM signals WHERE commit_hex=?",
            (commit,)).fetchone()

    def test_unrevealed_past_grace_forfeits_as_loss(self):
        v = self._validator()
        self._seal(v, "withheld", blob=None,
                   matured_ago_s=config.REVEAL_GRACE_S + 3600)
        v.forfeit_unrevealed()
        assert self._status(v, "withheld") == ("lost", "no_reveal")

    def test_blob_already_captured_is_never_forfeited(self):
        # validator fetched the blob (blob_json set) — it grades normally even
        # if the miner later deletes it; the forfeit pass must skip it.
        v = self._validator()
        self._seal(v, "pinned", blob='{"v":1}',
                   matured_ago_s=config.REVEAL_GRACE_S + 3600)
        v.forfeit_unrevealed()
        assert self._status(v, "pinned") == ("sealed", None)

    def test_within_grace_not_yet_forfeited(self):
        # round matured 30 min ago but grace (6h default) hasn't elapsed
        v = self._validator()
        self._seal(v, "fresh", blob=None, matured_ago_s=1800)
        v.forfeit_unrevealed()
        assert self._status(v, "fresh") == ("sealed", None)

    def test_round_not_matured_not_forfeited(self):
        v = self._validator()
        self._seal(v, "future", blob=None, matured_ago_s=-3600)  # matures in 1h
        v.forfeit_unrevealed()
        assert self._status(v, "future") == ("sealed", None)

    def test_forfeit_counts_as_decisive_loss_for_scoring(self):
        # the forfeit row must feed the trailing/elimination accounting like any
        # other loss (status='lost'), regardless of having no plaintext.
        v = self._validator()
        self._seal(v, "f1", blob=None, matured_ago_s=config.REVEAL_GRACE_S + 10)
        v.forfeit_unrevealed()
        decisive = v.db.execute(
            "SELECT COUNT(*) FROM signals WHERE hotkey=? AND status IN ('won','lost')",
            (HK,)).fetchone()[0]
        assert decisive == 1
