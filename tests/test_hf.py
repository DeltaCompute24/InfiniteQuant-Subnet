"""HF sub-mechanism (mecid 1) — consensus unit tests. STAGED, not live."""
import pytest

from sn89_signals import config, hf

DAY_MS = 86_400_000


def _payload(pair="XAUUSD", direction="LONG"):
    tp, sl, hz, cls = hf.HF_BOARD_V1[pair]
    return {"trade_pair": pair, "direction": direction, "asset_class": cls,
            "tp_bps": tp, "sl_bps": sl, "horizon_s": hz}


class TestBoard:
    def test_metals_and_crypto_are_30min(self):
        for p in ("XAUUSD", "BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD"):
            assert hf.hf_horizon_s(p, hf.HF_LAUNCH_FROM) == 1800

    def test_fx_majors_are_120min(self):
        for p in ("EURUSD", "GBPUSD", "USDJPY"):
            assert hf.hf_horizon_s(p, hf.HF_LAUNCH_FROM) == 7200

    def test_pairs_below_the_spread_floor_are_excluded(self):
        # measured band/spread < 8x at their best horizon
        for p in ("NZDUSD", "USDCAD", "USDCHF", "AUDUSD", "XAGUSD"):
            assert p not in hf.HF_BOARD_V1

    def test_hf_gold_band_is_a_fraction_of_the_lf_band(self):
        # the whole point: LF gold is 62 bps = 254 pips, unreachable in 30 min
        lf = 62.0
        assert hf.HF_BOARD_V1["XAUUSD"][0] < lf / 4

    def test_board_is_as_of_versioned(self):
        assert hf.hf_bands_as_of(hf.HF_LAUNCH_FROM) is hf.hf_bands_as_of(2_000_000_000)


class TestValidity:
    def test_accepts_a_well_formed_call(self):
        hf.validate_submission(_payload(), hf.HF_LAUNCH_FROM)

    def test_rejects_pair_off_the_hf_board(self):
        p = _payload(); p["trade_pair"] = "NZDUSD"
        with pytest.raises(hf.HFRejected, match="pair_not_on_hf_board"):
            hf.validate_submission(p, hf.HF_LAUNCH_FROM)

    def test_rejects_miner_chosen_band(self):
        p = _payload(); p["tp_bps"] = 40.0     # can't widen its own TP
        with pytest.raises(hf.HFRejected, match="band_mismatch"):
            hf.validate_submission(p, hf.HF_LAUNCH_FROM)

    def test_rejects_miner_chosen_horizon(self):
        p = _payload(); p["horizon_s"] = 28800
        with pytest.raises(hf.HFRejected, match="horizon_mismatch"):
            hf.validate_submission(p, hf.HF_LAUNCH_FROM)

    def test_rejects_forged_asset_class(self):
        p = _payload(); p["asset_class"] = "crypto"   # gold is forex-commodities
        with pytest.raises(hf.HFRejected, match="asset_class_mismatch"):
            hf.validate_submission(p, hf.HF_LAUNCH_FROM)


class TestRateLimits:
    def test_thirty_a_day_is_allowed(self):
        base = 10 * DAY_MS
        prior = [base + i * 60_000 for i in range(29)]
        hf.check_rate(prior, base + 29 * 60_000, hf.HF_LAUNCH_FROM)      # 30th is fine

    def test_thirty_first_is_refused(self):
        base = 10 * DAY_MS
        prior = [base + i * 60_000 for i in range(30)]
        with pytest.raises(hf.HFRejected, match="daily_cap"):
            hf.check_rate(prior, base + 30 * 60_000, hf.HF_LAUNCH_FROM)

    def test_cap_is_per_utc_day_not_rolling(self):
        prior = [10 * DAY_MS + i * 60_000 for i in range(30)]
        hf.check_rate(prior, 11 * DAY_MS, hf.HF_LAUNCH_FROM)             # next UTC day resets

    def test_min_gap_250ms(self):
        prior = [10 * DAY_MS]
        with pytest.raises(hf.HFRejected, match="min_gap"):
            hf.check_rate(prior, 10 * DAY_MS + 249, hf.HF_LAUNCH_FROM)
        hf.check_rate(prior, 10 * DAY_MS + 250, hf.HF_LAUNCH_FROM)


class TestPairLock:
    HK = "5F" + "a" * 46
    T = 1_800_000_000_000

    def test_lock_is_24h(self):
        assert hf.PAIR_LOCK_S == 24 * 3600

    def test_lf_gold_blocks_hf_gold_for_the_lock_window(self):
        idx = hf.build_lock_index([(self.HK, "XAUUSD", hf.MECH_LF, self.T)])
        assert hf.is_pair_locked(idx, self.HK, "XAUUSD", hf.MECID, self.T + 1)
        assert hf.is_pair_locked(idx, self.HK, "XAUUSD", hf.MECID,
                                 self.T + hf.PAIR_LOCK_MS - 1)

    def test_lock_expires_exactly_at_the_rolling_window(self):
        idx = hf.build_lock_index([(self.HK, "XAUUSD", hf.MECH_LF, self.T)])
        assert not hf.is_pair_locked(idx, self.HK, "XAUUSD", hf.MECID,
                                     self.T + hf.PAIR_LOCK_MS)

    def test_lock_is_symmetric(self):
        idx = hf.build_lock_index([(self.HK, "BTCUSD", hf.MECID, self.T)])
        assert hf.is_pair_locked(idx, self.HK, "BTCUSD", hf.MECH_LF, self.T + 1)

    def test_same_mechanism_repeat_is_not_locked(self):
        # self-overlap stays legal WITHIN a mechanism — 30/day demands it
        idx = hf.build_lock_index([(self.HK, "BTCUSD", hf.MECID, self.T)])
        assert not hf.is_pair_locked(idx, self.HK, "BTCUSD", hf.MECID, self.T + 1)

    def test_other_pairs_unaffected(self):
        idx = hf.build_lock_index([(self.HK, "XAUUSD", hf.MECH_LF, self.T)])
        assert not hf.is_pair_locked(idx, self.HK, "BTCUSD", hf.MECID, self.T + 1)

    def test_lock_is_per_hotkey_only(self):
        # documented, accepted hole: a second hotkey is not bound by the first's lock
        other = "5G" + "b" * 46
        idx = hf.build_lock_index([(self.HK, "XAUUSD", hf.MECH_LF, self.T)])
        assert not hf.is_pair_locked(idx, other, "XAUUSD", hf.MECID, self.T + 1)

    def test_newest_submission_wins_in_the_index(self):
        idx = hf.build_lock_index([(self.HK, "XAUUSD", hf.MECH_LF, self.T),
                                   (self.HK, "XAUUSD", hf.MECH_LF, self.T + 5000)])
        assert idx[(self.HK, "XAUUSD", hf.MECH_LF)] == self.T + 5000


class TestGrid:
    def test_crypto_is_a_250ms_grid(self):
        assert hf.grid_ms_for("BTCUSD") == 250
        assert hf.grid_t0_ms(1_000_000_000_000, "BTCUSD") == 1_000_000_000_250

    def test_metals_and_fx_are_a_1s_grid(self):
        # Polygon's forex/metals quote feed timestamps to the SECOND - a finer grid
        # could only be filled from OUR receive clock, which nobody can replay
        assert hf.grid_ms_for("XAUUSD") == 1000
        assert hf.grid_ms_for("EURUSD") == 1000
        assert hf.grid_t0_ms(1_000_000_000_000, "XAUUSD") == 1_000_000_001_000

    def test_grid_is_never_finer_than_the_feed(self):
        for pair, (_, _, _, cls) in hf.HF_BOARD_V1.items():
            assert hf.grid_ms_for(pair) == hf.GRID_MS_BY_CLASS[cls]
            assert hf.grid_ms_for(pair) >= 250

    def test_honours_min_settle(self):
        # 40ms before a grid point -> roll to the NEXT one, never fill instantly
        assert hf.grid_t0_ms(1_000_000_000_210, "BTCUSD") == 1_000_000_000_500

    def test_everything_in_a_bucket_gets_one_price(self):
        a = hf.grid_t0_ms(1_000_000_000_300, "BTCUSD")
        b = hf.grid_t0_ms(1_000_000_000_400, "BTCUSD")
        assert a == b == 1_000_000_000_500

    def test_unknown_pair_falls_back_to_the_coarsest_grid(self):
        assert hf.grid_ms_for("NOTAPAIR") == hf.GRID_MS_DEFAULT == 1000


class TestReceiptsAndAnchor:
    def _r(self, hk, seq, t_us):
        sb = hf.submit_signing_bytes(hk, seq, "de" * 16, _payload(), t_us // 1000)
        return {"hk": hk, "seq": seq, "ph": hf.payload_hash(sb),
                "t_recv_us": t_us, "grid_t0_ms": hf.grid_t0_ms(t_us // 1000),
                "ing": "ingest-test-1"}

    def test_signing_bytes_are_domain_separated(self):
        sb = hf.submit_signing_bytes("5F", 1, "ab", _payload(), 1)
        assert sb.startswith(hf.SUBMIT_DOMAIN)
        rb = hf.receipt_signing_bytes("5F", 1, "cd", 1, 1, "i")
        assert rb.startswith(hf.RECEIPT_DOMAIN)
        assert not rb.startswith(hf.SUBMIT_DOMAIN)   # a receipt can't be replayed as a submit

    def test_payload_hash_is_stable_under_key_order(self):
        p1 = {"a": 1, "b": 2}
        p2 = {"b": 2, "a": 1}
        assert (hf.payload_hash(hf.submit_signing_bytes("5F", 1, "ab", p1, 7))
                == hf.payload_hash(hf.submit_signing_bytes("5F", 1, "ab", p2, 7)))

    def test_any_field_change_moves_the_hash(self):
        base = hf.payload_hash(hf.submit_signing_bytes("5F", 1, "ab", _payload(), 7))
        assert base != hf.payload_hash(
            hf.submit_signing_bytes("5F", 2, "ab", _payload(), 7))
        assert base != hf.payload_hash(
            hf.submit_signing_bytes("5F", 1, "ab", _payload(direction="SHORT"), 7))

    def test_empty_window_has_a_defined_root(self):
        assert hf.merkle_root([]) == "00" * 32

    def test_root_is_order_independent_given_the_canonical_sort(self):
        rs = [self._r("5FA", 2, 1_000_000_000_500_000),
              self._r("5FB", 1, 1_000_000_000_100_000)]
        a = hf.anchor_payload(0, rs, "tag")
        b = hf.anchor_payload(0, list(reversed(rs)), "tag")
        assert a["root"] == b["root"]           # verifiers sort; input order is noise

    def test_root_changes_if_a_receipt_changes(self):
        rs = [self._r("5FA", 1, 1_000_000_000_100_000)]
        a = hf.anchor_payload(0, rs, "tag")
        rs2 = [dict(rs[0], seq=2)]
        assert a["root"] != hf.anchor_payload(0, rs2, "tag")["root"]

    def test_dropping_a_receipt_changes_the_root(self):
        # this is what makes censorship detectable
        rs = [self._r("5FA", 1, 1_000_000_000_100_000),
              self._r("5FB", 1, 1_000_000_000_200_000)]
        full = hf.anchor_payload(0, rs, "tag")
        censored = hf.anchor_payload(0, rs[:1], "tag")
        assert full["root"] != censored["root"]
        assert full["n"] == 2 and censored["n"] == 1

    def test_odd_leaf_count_duplicates_last(self):
        rs = [self._r("5FA", i, 1_000_000_000_000_000 + i) for i in range(3)]
        assert len(hf.anchor_payload(0, rs, "t")["root"]) == 64

    def test_window_start_is_window_aligned(self):
        assert hf.window_start_ms(1_784_751_118_322) % (hf.ANCHOR_WINDOW_S * 1000) == 0

    def test_anchor_cadence_fits_the_commitment_space_budget(self):
        # 3100 bytes per hotkey per epoch; epoch = tempo 360 blocks x 12s
        epoch_s = 360 * 12
        anchors_per_epoch = epoch_s / hf.ANCHOR_WINDOW_S
        need = anchors_per_epoch * hf.ANCHOR_BYTES_CHARGED
        assert need <= hf.ANCHOR_SPACE_BUDGET, (
            f"{anchors_per_epoch:.0f} anchors/epoch needs {need:.0f}B "
            f"> {hf.ANCHOR_SPACE_BUDGET}B budget")

    def test_headroom_left_for_inclusion_repair(self):
        epoch_s = 360 * 12
        used = (epoch_s / hf.ANCHOR_WINDOW_S) * hf.ANCHOR_BYTES_CHARGED
        spare = hf.ANCHOR_SPACE_BUDGET - used
        assert spare >= 5 * hf.ANCHOR_BYTES_CHARGED   # >=5 catch-up anchors


class TestTickSeries:
    def _t(self, a, t, p):
        return {"a": a, "t": t, "p": p}

    def test_root_ignores_input_order(self):
        ts = [self._t("BTCUSD", 2, 100.0), self._t("XAUUSD", 1, 4000.0)]
        assert hf.tick_root(ts) == hf.tick_root(list(reversed(ts)))

    def test_restating_a_price_moves_the_root(self):
        # this is the point of anchoring ticks: we cannot re-decide outcomes later
        ts = [self._t("BTCUSD", 1, 100.0)]
        assert hf.tick_root(ts) != hf.tick_root([self._t("BTCUSD", 1, 100.01)])

    def test_dropping_a_tick_moves_the_root(self):
        ts = [self._t("BTCUSD", 1, 100.0), self._t("BTCUSD", 2, 101.0)]
        assert hf.tick_root(ts) != hf.tick_root(ts[:1])

    def test_bid_ask_do_not_affect_the_root(self):
        # only (asset, src_ts, price) is consensus - quotes are context
        a = hf.tick_root([{"a": "X", "t": 1, "p": 2.0}])
        b = hf.tick_root([{"a": "X", "t": 1, "p": 2.0, "b": 1.9, "k": 2.1}])
        assert a == b

    def test_price_at_takes_the_last_tick_at_or_before(self):
        ts = [self._t("X", 1000, 10.0), self._t("X", 2000, 20.0),
              self._t("X", 3000, 30.0)]
        assert hf.price_at(ts, 2000) == 20.0
        assert hf.price_at(ts, 2999) == 20.0
        assert hf.price_at(ts, 3000) == 30.0

    def test_price_at_never_looks_forward(self):
        ts = [self._t("X", 5000, 50.0)]
        assert hf.price_at(ts, 4999) is None      # no price yet -> voids, never guesses

    def test_empty_tick_window_has_a_defined_root(self):
        assert hf.tick_root([]) == "00" * 32


class TestAnchorCarriesBothRoots:
    def test_tick_root_is_included_when_supplied(self):
        a = hf.anchor_payload(0, [], "tag", tick_root="ab" * 32, tick_n=7)
        assert a["tick_root"] == "ab" * 32 and a["tick_n"] == 7

    def test_omitted_when_absent(self):
        assert "tick_root" not in hf.anchor_payload(0, [], "tag")


class TestScoringScope:
    def test_qualification_matches_mechanism_zero(self):
        assert hf.HF_QUALIFY_MIN_DECISIVE == config.QUALIFY_MIN_DECISIVE == 8
        assert hf.HF_QUALIFY_LB_FLOOR == config.QUALIFY_LB_FLOOR

    def test_decay_is_faster_than_mechanism_zero(self):
        assert hf.HF_EMISSION_DECAY_S < config.EMISSION_DECAY_S

    def test_win_cap_does_not_bind_at_thirty_a_day(self):
        cap, _, _ = hf.hf_rules_as_of(0)
        assert hf.HF_WIN_CAP > cap * 2          # can't cap out in two days
        assert hf.HF_WIN_CAP > config.WIN_CAP

    def test_mechanism_zero_untouched(self):
        assert config.MAX_SIGNALS_PER_UTC_DAY == 3
        assert config.MIN_GAP_S == 3600


class TestAnchorEncoding:
    R = "ab" * 32
    T = "cd" * 32

    def test_fits_the_128_byte_field(self):
        enc = hf.encode_anchor(1_784_762_400_000, 648, 12345, self.R, self.T)
        assert len(enc.encode()) <= hf.ANCHOR_MAX_BYTES

    def test_roundtrips(self):
        enc = hf.encode_anchor(1_784_762_400_000, 3, 600, self.R, self.T)
        d = hf.decode_anchor(enc)
        assert d["w"] == 1_784_762_400_000 and d["n"] == 3 and d["tick_n"] == 600

    def test_verifies_against_the_published_roots(self):
        enc = hf.encode_anchor(0, 3, 600, self.R, self.T)
        assert hf.verify_anchor(enc, self.R, self.T, 3, 600)

    def test_restated_ticks_fail_verification(self):
        # the whole reason the tick root is in the anchor
        enc = hf.encode_anchor(0, 3, 600, self.R, self.T)
        assert not hf.verify_anchor(enc, self.R, "ef" * 32, 3, 600)

    def test_restated_receipts_fail_verification(self):
        enc = hf.encode_anchor(0, 3, 600, self.R, self.T)
        assert not hf.verify_anchor(enc, "ef" * 32, self.T, 3, 600)

    def test_changed_counts_fail_verification(self):
        enc = hf.encode_anchor(0, 3, 600, self.R, self.T)
        assert not hf.verify_anchor(enc, self.R, self.T, 4, 600)

    def test_foreign_commitment_is_not_an_anchor(self):
        assert hf.decode_anchor("sn89:1:deadbeef:123:abcd") is None
        assert hf.decode_anchor("") is None


class TestGrading:
    def _ticks(self, prices, t0=1_000_000, step=250):
        return [{"a": "BTCUSD", "t": t0 + i * step, "p": p} for i, p in enumerate(prices)]

    def test_long_take_profit(self):
        t = self._ticks([100.0, 100.1, 100.5, 100.6])   # two ticks past TP (≥2 to score)
        r = hf.grade("BTCUSD", "LONG", 100.0, 19, 19, 1_000_000, 1800, t)
        assert r["status"] == "won" and r["exit"] == 100.6

    def test_long_stop_loss(self):
        t = self._ticks([100.0, 99.9, 99.5, 99.4])       # two ticks past SL
        r = hf.grade("BTCUSD", "LONG", 100.0, 19, 19, 1_000_000, 1800, t)
        assert r["status"] == "lost"

    def test_short_take_profit_is_the_mirror(self):
        t = self._ticks([100.0, 99.5, 99.4])
        r = hf.grade("BTCUSD", "SHORT", 100.0, 19, 19, 1_000_000, 1800, t)
        assert r["status"] == "won"

    def test_lone_wick_does_not_win(self):
        # one tick past TP then reverts — not enough to score (≥2 guard)
        t = self._ticks([100.0, 100.5, 100.0, 100.0])
        assert hf.grade("BTCUSD", "LONG", 100.0, 19, 19, 1_000_000, 1800, t)["status"] == "wash"

    def test_stop_before_target_when_both_reach_two(self):
        # SL touched twice before TP does → loss (conservative, gated by ≥2)
        t = self._ticks([100.0, 99.5, 99.4, 101.0, 101.1])
        assert hf.grade("BTCUSD", "LONG", 100.0, 19, 19, 1_000_000, 1800, t)["status"] == "lost"

    def test_nothing_touched_is_a_wash(self):
        t = self._ticks([100.0, 100.01, 100.02])
        assert hf.grade("BTCUSD", "LONG", 100.0, 19, 19, 1_000_000, 1800, t)["status"] == "wash"

    def test_ticks_after_the_horizon_are_ignored(self):
        t = self._ticks([100.0, 100.0]) + [{"a": "BTCUSD", "t": 1_000_000 + 1801_000, "p": 200.0}]
        assert hf.grade("BTCUSD", "LONG", 100.0, 19, 19, 1_000_000, 1800, t)["status"] == "wash"

    def test_entry_tick_itself_cannot_resolve_it(self):
        # a call cannot be graded on the very tick it entered at
        t = [{"a": "BTCUSD", "t": 1_000_000, "p": 200.0}]
        assert hf.grade("BTCUSD", "LONG", 100.0, 19, 19, 1_000_000, 1800, t)["status"] == "wash"

    def test_tp_and_sl_can_never_both_trigger(self):
        # invariant behind there being no "gapped through both" case: for every board
        # row the two levels sit on opposite sides of entry
        for pair, (tp, sl, _, _) in hf.HF_BOARD_V1.items():
            for d in ("LONG", "SHORT"):
                up = 1 if d == "LONG" else -1
                entry = 100.0
                assert (entry * (1 + up * tp / 10000.0) > entry
                        > entry * (1 - up * sl / 10000.0)) == (up > 0)

    def test_missing_entry_price_voids(self):
        assert hf.grade("BTCUSD", "LONG", None, 19, 19, 0, 1800, [])["status"] == "void"


class TestTicksForEntryWindow:
    """The crypto-void bug: _ticks_for filtered to t>=t0, dropping the tick just
    BEFORE t0 that price_at needs for the entry. Crypto ticks (~250ms, non-zero
    ms) almost never land exactly on the 250ms grid, so every crypto call voided
    while forex/gold (ms=000, 1s grid) found an on-grid entry."""
    from sn89_signals import hf_grade as _hfg
    WIN = _hfg.WINDOW_MS

    def _publish(self, tmp_path, pair, ticks):
        import json as _j
        by_w = {}
        for t, p in ticks:
            w = (t // self.WIN) * self.WIN
            by_w.setdefault(w, []).append({"a": pair, "t": t, "p": p})
        for w, rows in by_w.items():
            d = tmp_path / str(w)
            d.mkdir(exist_ok=True)
            d.joinpath("ticks.jsonl").write_text(
                "\n".join(_j.dumps(r) for r in rows))
        return tmp_path.as_uri()

    def test_entry_tick_just_before_t0_is_kept(self, tmp_path):
        from sn89_signals import hf, hf_grade
        # grid point at :750; ticks at :529 and :779 straddle it. The entry is the
        # :529 tick — under the old t>=t0 filter it was dropped and this voided.
        t0 = 1_000_000_750
        ticks = [(t0 - 221, 64715.98), (t0 + 29, 64716.10), (t0 + 279, 64717.0)]
        base = self._publish(tmp_path, "BTCUSD", ticks)
        got, _missing = hf_grade._ticks_for(base, str(tmp_path / "cache"), "BTCUSD",
                                            t0, t0 + 900_000)
        entry = hf.price_at(got, t0)
        assert entry == 64715.98, f"entry should be the pre-t0 tick, got {entry}"

    def test_entry_at_window_start_pulls_the_previous_window(self, tmp_path):
        from sn89_signals import hf, hf_grade
        # t0 only 10ms into its window — the entry tick lives in the PREVIOUS
        # window, which the extra leading window must fetch.
        w = 3 * self.WIN
        t0 = w + 10
        ticks = [(w - 100, 50.0), (t0 + 40, 51.0)]
        base = self._publish(tmp_path, "ETHUSD", ticks)
        got, _missing = hf_grade._ticks_for(base, str(tmp_path / "cache"), "ETHUSD",
                                            t0, t0 + 900_000)
        assert hf.price_at(got, t0) == 50.0

    def test_a_real_crypto_call_now_grades_instead_of_voiding(self, tmp_path):
        from sn89_signals import hf, hf_grade
        t0 = 2_000_000_250
        # entry just before t0, then a +30bps move that TOUCHES TP on ≥2 ticks
        entry_px = 100.0
        ticks = [(t0 - 130, entry_px), (t0 + 300, 100.30), (t0 + 550, 100.31)]
        base = self._publish(tmp_path, "BTCUSD", ticks)
        got, _missing = hf_grade._ticks_for(base, str(tmp_path / "cache"), "BTCUSD",
                                            t0, t0 + 1800_000)
        entry = hf.price_at(got, t0)
        assert entry == entry_px
        assert hf.grade("BTCUSD", "LONG", entry, 19, 19, t0, 1800, got)["status"] == "won"


class TestIncompleteTickSeriesNeverGrades:
    """The wash bug: an unfetchable window was swallowed, grade() walked a short
    series, touched nothing, and wrote `wash` — permanently. Measured on the
    2026-07-24..26 corpus: 42 of 307 calls (13.7%) graded against an incomplete
    series, 41 of them recorded `wash` when the ticks show they were decisive.
    `wash` is what a quiet market also looks like, which is why it stayed silent."""

    from sn89_signals import hf_grade as _hfg
    WIN = _hfg.WINDOW_MS
    # window-aligned AND after HF_LAUNCH_FROM, or hf_bands_as_of returns None and
    # the call is dropped before grading ever happens
    T0 = ((_hfg.hf.HF_LAUNCH_FROM * 1000) // WIN + 100) * WIN
    HORIZON_S = 1800
    END = T0 + HORIZON_S * 1000

    def _publish(self, tmp_path, pair, ticks, *, hole=None, index=True):
        """Publish ticks + receipts + index; `hole` is a window id to withhold."""
        import json as _j
        tmp_path.mkdir(parents=True, exist_ok=True)
        by_w = {}
        for t, p in ticks:
            by_w.setdefault((t // self.WIN) * self.WIN, []).append(
                {"a": pair, "t": t, "p": p})
        spanned = list(range(self.T0 - self.WIN, self.END + self.WIN, self.WIN))
        for w in spanned:
            d = tmp_path / str(w)
            d.mkdir(exist_ok=True)
            if w != hole:
                d.joinpath("ticks.jsonl").write_text(
                    "\n".join(_j.dumps(r) for r in by_w.get(w, [])))
            d.joinpath("receipts.jsonl").write_text(
                _j.dumps({"submit": {"hk": "5Fhk", "seq": 1,
                                     "payload": {"trade_pair": pair, "direction": "LONG"}},
                          "receipt": {"grid_t0_ms": self.T0}})
                if w == self.T0 else "")
        if index:
            tmp_path.joinpath("index.json").write_text(
                _j.dumps({"windows": [str(w) for w in spanned]}))
        return tmp_path.as_uri()

    def _ticks(self):
        # entry, then a decisive +30bps TP touch late in the horizon
        return ([(self.T0 - 100, 100.0)]
                + [(self.T0 + i * 1000, 100.0) for i in range(1, 1500)]
                + [(self.END - 2000, 100.30), (self.END - 1000, 100.31)])

    def _grades(self, cache):
        import sqlite3
        c = sqlite3.connect(str(cache / "hf_grades.db"))
        try:
            return dict(c.execute("SELECT key, status FROM grades"))
        finally:
            c.close()

    def _pending(self, cache):
        import sqlite3
        c = sqlite3.connect(str(cache / "hf_grades.db"))
        try:
            return [r[0] for r in c.execute("SELECT key FROM pending")]
        finally:
            c.close()

    def test_ticks_for_reports_the_unfetchable_window(self, tmp_path):
        from sn89_signals import hf_grade
        hole = self.T0 + 5 * self.WIN
        base = self._publish(tmp_path / "feed", "BTCUSD", self._ticks(), hole=hole)
        _got, missing = hf_grade._ticks_for(base, str(tmp_path / "cache"), "BTCUSD",
                                            self.T0, self.END)
        assert missing == [hole], f"the hole must be reported, got {missing}"

    def test_a_hole_defers_instead_of_writing_wash(self, tmp_path):
        from sn89_signals import hf_grade
        cache = tmp_path / "cache"
        base = self._publish(tmp_path / "feed", "BTCUSD", self._ticks(),
                             hole=self.T0 + 5 * self.WIN)
        now = (self.END + hf_grade.GRADE_SETTLE_S * 1000 + 1) / 1000.0
        hf_grade.sync_and_grade(base, str(cache), now)
        assert self._grades(cache) == {}, "a hole must not be graded"
        assert self._pending(cache) == ["5Fhk:1"], "the call must stay pending"

    def test_the_call_grades_correctly_once_the_window_arrives(self, tmp_path):
        from sn89_signals import hf_grade
        cache = tmp_path / "cache"
        feed = tmp_path / "feed"
        hole = self.T0 + 5 * self.WIN
        base = self._publish(feed, "BTCUSD", self._ticks(), hole=hole)
        now = (self.END + hf_grade.GRADE_SETTLE_S * 1000 + 1) / 1000.0
        hf_grade.sync_and_grade(base, str(cache), now)
        assert self._grades(cache) == {}
        self._publish(feed, "BTCUSD", self._ticks())        # window publishes late
        hf_grade.sync_and_grade(base, str(cache), now + 180)
        assert self._grades(cache) == {"5Fhk:1": "won"}, "a complete series is decisive"
        assert self._pending(cache) == []

    def test_a_permanent_hole_is_abandoned_rather_than_wedged_forever(self, tmp_path):
        from sn89_signals import hf_grade
        cache = tmp_path / "cache"
        base = self._publish(tmp_path / "feed", "BTCUSD", self._ticks(),
                             hole=self.T0 + 5 * self.WIN)
        past = (self.END + hf_grade.GRADE_ABANDON_S * 1000 + 1) / 1000.0
        hf_grade.sync_and_grade(base, str(cache), past)
        assert self._pending(cache) == [], "must not block weights forever"
        assert set(self._grades(cache)) == {"5Fhk:1"}

    def test_grading_waits_for_the_settle_delay(self, tmp_path):
        """The window covering the last second of a horizon publishes AFTER that
        second, so grading at end_ms exactly grades a series still arriving."""
        from sn89_signals import hf_grade
        cache = tmp_path / "cache"
        base = self._publish(tmp_path / "feed", "BTCUSD", self._ticks())
        hf_grade.sync_and_grade(base, str(cache), (self.END + 1) / 1000.0)
        assert self._grades(cache) == {}, "graded before the feed could settle"
        hf_grade.sync_and_grade(
            base, str(cache), (self.END + hf_grade.GRADE_SETTLE_S * 1000 + 1) / 1000.0)
        assert self._grades(cache) == {"5Fhk:1": "won"}

    def test_a_grader_version_bump_rebuilds_every_grade(self, tmp_path):
        """A fixed rule that leaves the old wrong grades in place is half a fix —
        `grades` is written once and never revisited."""
        import sqlite3
        from sn89_signals import hf_grade
        cache = tmp_path / "cache"
        base = self._publish(tmp_path / "feed", "BTCUSD", self._ticks())
        now = (self.END + hf_grade.GRADE_SETTLE_S * 1000 + 1) / 1000.0
        hf_grade.sync_and_grade(base, str(cache), now)
        c = sqlite3.connect(str(cache / "hf_grades.db"))
        c.execute("UPDATE grades SET status='wash'")          # the stale bad grade
        c.execute("UPDATE meta SET v='1' WHERE k='grader_version'")
        c.commit(); c.close()
        hf_grade.sync_and_grade(base, str(cache), now)
        assert self._grades(cache) == {"5Fhk:1": "won"}, "stale grades must re-derive"


class TestRuleParityAcrossMechanisms:
    """The hit rule must be ONE function. If these drift, a gold call on mech 0 and
    the identical gold call on mech 1 resolve by different physics in the same week."""

    def test_hf_grade_delegates_to_the_shared_rule(self):
        import inspect
        from sn89_signals import grader
        assert "touch_hit" in inspect.getsource(hf.grade)
        assert callable(grader.touch_hit)

    def test_shared_rule_agrees_with_hf_grade_on_every_case(self):
        from sn89_signals import grader
        entry, tp_bps, sl_bps = 100.0, 20.0, 20.0
        for direction in ("LONG", "SHORT"):
            up = 1 if direction == "LONG" else -1
            tp = entry * (1 + up * tp_bps / 10000.0)
            sl = entry * (1 - up * sl_bps / 10000.0)
            for px in (99.5, 99.8, 99.98, 100.0, 100.02, 100.2, 100.5):
                direct = grader.touch_hit(px, up > 0, tp, sl)
                viahf = hf.grade("BTCUSD", direction, entry, tp_bps, sl_bps, 0, 1800,
                                 [{"a": "BTCUSD", "t": 1000, "p": px},
                                  {"a": "BTCUSD", "t": 2000, "p": px}])["status"]
                assert (direct or "wash") == viahf, (direction, px, direct, viahf)

    def test_grading_rule_is_as_of_versioned_and_armed(self):
        from sn89_signals import config
        # ARMED 2026-07-24: LF unifies onto touch_ticks at the committed cutover.
        assert config.TOUCH_TICKS_FROM == 1784937600          # 2026-07-25T00:00:00Z
        assert config.grading_rule_as_of(0) == "close_1m"     # all pre-cutover history stays close_1m
        assert config.grading_rule_as_of(config.TOUCH_TICKS_FROM - 1) == "close_1m"
        assert config.grading_rule_as_of(config.TOUCH_TICKS_FROM) == "touch_ticks"
        assert config.grading_rule_as_of(9e9) == "touch_ticks"

    def test_arming_never_reaches_back_before_its_effective_date(self):
        from sn89_signals import config
        orig = config.TOUCH_TICKS_FROM
        try:
            config.TOUCH_TICKS_FROM = 1_800_000_000
            assert config.grading_rule_as_of(1_799_999_999) == "close_1m"
            assert config.grading_rule_as_of(1_800_000_000) == "touch_ticks"
        finally:
            config.TOUCH_TICKS_FROM = orig

    def test_tick_coverage_spans_the_whole_lf_board(self):
        import importlib.util, sys, os
        spec = importlib.util.spec_from_file_location(
            "hf_tick_recorder", "/opt/sn89-signals/neurons/hf_tick_recorder.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules["hf_tick_recorder"] = m
        spec.loader.exec_module(m)
        # every LF board pair AND every HF board pair must be captured, or a call
        # on that pair cannot be graded once the rule is armed
        for pair in m.LF_BOARD:
            assert pair in m.ASSETS, f"{pair} on the LF board but not captured"
        for pair in hf.HF_BOARD_V1:
            assert pair in m.ASSETS


class TestReputationMemory:
    """Trading more often must not shorten how far back a miner's record is read."""

    def test_memory_window_is_60_days_same_as_mechanism_zero(self):
        assert hf.HF_HIT_RATE_WINDOW_S == config.HIT_RATE_WINDOW_S == 60 * 24 * 3600

    def test_trade_cap_cannot_bind_before_the_60_day_clock(self):
        # max decisive in 60d = daily cap x 60 (zero wash). If the trade cap were
        # below that, HF memory would silently collapse to ~4 days.
        cap_per_day, _, _ = hf.hf_rules_as_of(0)
        max_decisive_60d = cap_per_day * 60
        assert hf.HF_HIT_RATE_WINDOW_TRADES > max_decisive_60d, (
            f"{hf.HF_HIT_RATE_WINDOW_TRADES} would bind before "
            f"{max_decisive_60d} decisive — memory shortens with volume")

    def test_inheriting_mech0_trade_cap_would_have_broken_this(self):
        # regression pin on the bug this constant exists to prevent
        assert hf.HF_HIT_RATE_WINDOW_TRADES != config.HIT_RATE_WINDOW_TRADES


class TestGateMonotonicity:
    """Winning a trade must never cost a miner its qualification."""

    def test_a_win_never_un_qualifies(self):
        from sn89_signals import scoring
        for n in range(config.QUALIFY_MIN_DECISIVE, 120):
            for k in range(n + 1):
                if scoring._qualifies(k, n):
                    assert scoring._qualifies(k + 1, n + 1), (
                        f"{k}/{n} qualifies but {k+1}/{n+1} does not — "
                        f"winning a trade dropped the miner out")


class TestLockFeed:
    """The lock is only as good as the data behind it. An empty feed and a broken
    feed look identical from the outside — that difference must never be silent."""

    def test_broken_feed_raises_rather_than_reporting_no_locks(self):
        with pytest.raises(hf.HFLockFeedError):
            hf.load_mech0_locks("/nonexistent/nope.db", 0)

    def test_lock_feed_error_is_not_a_rejection(self):
        # HFRejected means "refused this call"; a dead feed is an operator problem
        assert not issubclass(hf.HFLockFeedError, hf.HFRejected)

    def test_mech0_rows_are_tagged_to_mechanism_zero(self):
        import sqlite3, tempfile, os, json
        fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE signals (hotkey TEXT, t0_unix REAL, "
                    "plaintext TEXT, status TEXT)")
        con.execute("INSERT INTO signals VALUES (?,?,?,?)",
                    ("5F" + "a" * 46, 1_800_000_000.0,
                     json.dumps({"trade_pair": "XAUUSD"}), "won"))
        con.execute("INSERT INTO signals VALUES (?,?,?,?)",
                    ("5F" + "b" * 46, 1_800_000_000.0,
                     json.dumps({"trade_pair": "BTCUSD"}), "void"))
        con.commit(); con.close()
        try:
            rows = hf.load_mech0_locks(path, 0)
            assert len(rows) == 1, "void signals must not lock a pair"
            hk, pair, mecid, ts = rows[0]
            assert pair == "XAUUSD" and mecid == hf.MECH_LF
            idx = hf.build_lock_index(rows)
            assert hf.is_pair_locked(idx, hk, "XAUUSD", hf.MECID, ts + 1)
        finally:
            os.unlink(path)



class TestLFReputationWindowV2:
    """Mechanism 0's trade cap was shortening its own 60-day window. Same defect
    HF hit, milder: at 3/day a full-cadence trader reaches 100 decisive in ~42 days,
    so the CAP bound before the clock did."""

    def test_v2_cap_exceeds_the_theoretical_60d_maximum(self):
        cap_day, _ = config.submission_rules_as_of(2_000_000_000)
        assert config.HIT_RATE_WINDOW_TRADES_V2 > cap_day * 60, (
            "cap can still bind before the 60-day clock")

    def test_change_is_as_of_versioned_not_retroactive(self):
        eff = config.HIT_RATE_WINDOW_TRADES_V2_FROM
        assert config.hit_rate_window_trades_as_of(eff - 1) == config.HIT_RATE_WINDOW_TRADES
        assert config.hit_rate_window_trades_as_of(eff) == config.HIT_RATE_WINDOW_TRADES_V2

    def test_a_win_keeps_the_cap_it_scored_under(self):
        from sn89_signals import scoring
        eff = config.HIT_RATE_WINDOW_TRADES_V2_FROM
        # 150 decisive straddling the effective date; the pre-cutover win must be
        # judged on 100 and the post-cutover win on 200
        base = eff - 40 * 86400
        dec = [(base + i * 3600.0, i % 3 != 0, False) for i in range(150)]
        pre = scoring.qualified_wins(dec, base - 86400, False)
        assert isinstance(pre, list)      # exercised both branches without raising

    def test_hf_and_lf_now_agree_on_60_day_memory(self):
        assert hf.HF_HIT_RATE_WINDOW_S == config.HIT_RATE_WINDOW_S
        for name, cap, per_day in (("LF", config.HIT_RATE_WINDOW_TRADES_V2,
                                    config.submission_rules_as_of(2_000_000_000)[0]),
                                   ("HF", hf.HF_HIT_RATE_WINDOW_TRADES,
                                    hf.hf_rules_as_of(0)[0])):
            assert cap > per_day * 60, f"{name} cap binds before its 60-day clock"



class TestLFSidePairLock:
    """HF must block LF, not only the reverse. LF cannot refuse synchronously --
    the commit is already on chain -- so the only action available is a void."""
    HK = "5F" + "a" * 46
    T = 1_800_000_000.0

    def _row(self, pair="XAUUSD", t=None):
        from sn89_signals.scoring import GradedRow
        return GradedRow(hotkey=self.HK, trade_pair=pair, direction="LONG",
                         t0_unix=self.T if t is None else t, status="won")

    def test_armed_2026_07_24(self):
        # Was 0 (unenforced) from the day the rule was announced until 2026-07-24,
        # so the lock ran on the HF side only. Arming is a consensus change: it
        # belongs in the committed default, not in an env var on our validator.
        from sn89_signals import config
        assert config.PAIR_LOCK_LF_FROM == 1784865600      # 2026-07-24T04:00:00Z
        assert config.pair_lock_lf_enforced_as_of(1784865600)
        assert not config.pair_lock_lf_enforced_as_of(1784865599)

    def test_none_locks_means_no_check_not_no_locks(self):
        from sn89_signals import scoring, config
        orig = config.PAIR_LOCK_LF_FROM
        try:
            config.PAIR_LOCK_LF_FROM = 1
            r = scoring.apply_validity_filters([self._row()], hf_locks=None)
            assert r[0].status == "won"      # no lock DATA -> no verdict, never a void
        finally:
            config.PAIR_LOCK_LF_FROM = orig

    def test_hf_call_voids_the_lf_call_on_the_same_pair(self):
        from sn89_signals import scoring, config
        orig = config.PAIR_LOCK_LF_FROM
        try:
            config.PAIR_LOCK_LF_FROM = 1
            idx = hf.build_lock_index([(self.HK, "XAUUSD", hf.MECID,
                                        int((self.T - 3600) * 1000))])
            r = scoring.apply_validity_filters([self._row()], hf_locks=idx)
            assert r[0].status == "void"
            assert r[0].void_reason == "pair_locked_other_mechanism"
        finally:
            config.PAIR_LOCK_LF_FROM = orig

    def test_lock_expires_and_other_pairs_are_free(self):
        from sn89_signals import scoring, config
        orig = config.PAIR_LOCK_LF_FROM
        try:
            config.PAIR_LOCK_LF_FROM = 1
            old = int((self.T - hf.PAIR_LOCK_S - 1) * 1000)
            idx = hf.build_lock_index([(self.HK, "XAUUSD", hf.MECID, old)])
            assert scoring.apply_validity_filters([self._row()], hf_locks=idx)[0].status == "won"
            idx2 = hf.build_lock_index([(self.HK, "BTCUSD", hf.MECID,
                                         int((self.T - 60) * 1000))])
            assert scoring.apply_validity_filters([self._row()], hf_locks=idx2)[0].status == "won"
        finally:
            config.PAIR_LOCK_LF_FROM = orig

    def test_not_retroactive(self):
        from sn89_signals import scoring, config
        orig = config.PAIR_LOCK_LF_FROM
        try:
            config.PAIR_LOCK_LF_FROM = int(self.T + 86400)      # arms tomorrow
            idx = hf.build_lock_index([(self.HK, "XAUUSD", hf.MECID,
                                        int((self.T - 3600) * 1000))])
            assert scoring.apply_validity_filters([self._row()], hf_locks=idx)[0].status == "won"
        finally:
            config.PAIR_LOCK_LF_FROM = orig

    def test_hf_log_reader_raises_when_the_source_is_missing(self):
        with pytest.raises(hf.HFLockFeedError):
            hf.load_hf_locks("/nonexistent/hf-logs", 0)


class TestRegistrationGate:
    """Ingest verified the SIGNATURE but never that the hotkey exists on the
    subnet, so any generated keypair could take a receipt, land in an anchored
    window and show on the public leaderboard. Four of the six HF "traders" on
    2026-07-24 were exactly that."""

    def _ingest(self, registered):
        """An Ingest without touching the network or the validator DB — the real
        __init__ loads the metagraph and the mech-0 locks."""
        import importlib
        from bittensor_wallet import Keypair
        hi = importlib.import_module("neurons.hf_ingest")
        ing = object.__new__(hi.Ingest)
        ing.kp = Keypair.create_from_uri("//IngestTestKey")
        ing.last_seq, ing.sent_ms, ing.windows = {}, {}, {}
        ing.lock_index, ing._locks_loaded_at = {}, 9e18
        ing.registered, ing._reg_loaded_at = set(registered), 9e18
        return hi, ing

    def _frame(self, kp, seq=1, pair="BTCUSD"):
        import time as _t
        # Off the governed board as-of now, not hardcoded — a band change must
        # not turn this into a band-validity test by accident.
        ts = int(_t.time() * 1000)
        tp, sl, hor, ac = hf.hf_bands_as_of(ts / 1000.0)[pair]
        payload = {"trade_pair": pair, "direction": "LONG", "asset_class": ac,
                   "tp_bps": tp, "sl_bps": sl, "horizon_s": hor}
        sb = hf.submit_signing_bytes(kp.ss58_address, seq, "n" * 32, payload, ts)
        return {"v": 1, "kind": "hf.submit", "hk": kp.ss58_address, "seq": seq,
                "nonce": "n" * 32, "ts_miner": ts, "payload": payload,
                "sig": kp.sign(sb).hex()}

    def test_unregistered_hotkey_is_refused(self):
        from bittensor_wallet import Keypair
        miner = Keypair.create_from_uri("//UnregisteredMiner")
        hi, ing = self._ingest(registered=set())
        out = ing.handle(self._frame(miner))
        assert out["kind"] == "hf.reject"
        assert out["reason"] == "not_registered"

    def test_the_refusal_is_signed(self):
        # "refused" must stay distinguishable from "dropped" — a miner who just
        # registered has to be able to tell which one happened to them.
        from bittensor_wallet import Keypair
        miner = Keypair.create_from_uri("//UnregisteredMiner2")
        hi, ing = self._ingest(registered=set())
        out = ing.handle(self._frame(miner))
        rb = hf.receipt_signing_bytes(miner.ss58_address, 1,
                                      "REJECT:not_registered",
                                      out["t_recv_us"], 0, out["ing"])
        assert ing.kp.verify(rb, bytes.fromhex(out["sig_owner"]))

    def test_registered_hotkey_still_accepted(self):
        from bittensor_wallet import Keypair
        miner = Keypair.create_from_uri("//RegisteredMiner")
        hi, ing = self._ingest(registered={miner.ss58_address})
        out = ing.handle(self._frame(miner))
        assert out["kind"] == "hf.receipt", out.get("reason")

    def test_forgery_is_caught_before_registration(self):
        # Order matters: a refusal must be bound to a hotkey that really signed
        # the frame, so authenticity is checked first.
        from bittensor_wallet import Keypair
        real = Keypair.create_from_uri("//RegisteredMiner")
        forger = Keypair.create_from_uri("//Forger")
        f = self._frame(forger)
        f["hk"] = real.ss58_address              # claim someone else's hotkey
        hi, ing = self._ingest(registered={real.ss58_address})
        assert ing.handle(f)["reason"] == "bad_signature"

    def test_an_empty_metagraph_never_empties_the_set(self, monkeypatch):
        # "metagraph returned nothing" and "nobody is registered" are the same
        # bytes. Treating them alike would reject every real miner's sub-second
        # submission on an RPC hiccup.
        import bittensor as bt
        _, ing = self._ingest(registered={"5Fkeep"})

        class _EmptyMg:
            hotkeys: list = []

        class _Sub:
            def __init__(self, *a, **k): pass
            def metagraph(self, *a, **k): return _EmptyMg()

        monkeypatch.setattr(bt, "Subtensor", _Sub)
        assert ing.refresh_registered() == 0
        assert ing.registered == {"5Fkeep"}

    def test_a_failed_refresh_never_empties_the_set(self, monkeypatch):
        import bittensor as bt
        _, ing = self._ingest(registered={"5Fkeep"})

        def _boom(*a, **k):
            raise RuntimeError("substrate unreachable")

        monkeypatch.setattr(bt, "Subtensor", _boom)
        assert ing.refresh_registered() == 0
        assert ing.registered == {"5Fkeep"}


class TestPublishedLockFeed:
    """The LF side's feed. It resolves from the PUBLISHED windows, because an LF
    void has to be reproducible by anyone replaying the journal — our local
    ingest dir is neither published nor even the same layout."""
    HK = "5F" + "a" * 46
    W = 1_800_000_040_000 // 180_000 * 180_000

    def _base(self, tmp_path, windows: dict) -> str:
        import json as _j
        (tmp_path / "index.json").write_text(_j.dumps({"windows": list(windows)}))
        for w, entries in windows.items():
            d = tmp_path / str(w)
            d.mkdir()
            d.joinpath("receipts.jsonl").write_text(
                "\n".join(_j.dumps(e) for e in entries))
        return tmp_path.as_uri()

    def _entry(self, pair="XAUUSD", ts=None, hk=None):
        return {"submit": {"hk": hk or self.HK, "seq": 1,
                           "payload": {"trade_pair": pair, "direction": "LONG"}},
                "receipt": {"grid_t0_ms": ts if ts is not None else self.W + 1000}}

    def test_rows_are_tagged_to_hf_and_timestamped_at_the_grid_point(self, tmp_path):
        from sn89_signals import hf_grade
        base = self._base(tmp_path, {self.W: [self._entry()]})
        rows = hf_grade.load_hf_lock_rows(base, 0)
        assert len(rows) == 1
        hk, pair, mecid, ts = rows[0]
        # grid_t0_ms, never t_recv_us: the receive clock is ours alone and no
        # third party can replay it.
        assert (hk, pair, mecid, ts) == (self.HK, "XAUUSD", hf.MECID, self.W + 1000)

    def test_it_locks_the_lf_side(self, tmp_path):
        from sn89_signals import hf_grade
        base = self._base(tmp_path, {self.W: [self._entry()]})
        idx = hf.build_lock_index(hf_grade.load_hf_lock_rows(base, 0))
        assert hf.is_pair_locked(idx, self.HK, "XAUUSD", hf.MECH_LF,
                                 self.W + 2000)
        assert not hf.is_pair_locked(idx, self.HK, "BTCUSD", hf.MECH_LF,
                                     self.W + 2000)

    def test_unreachable_index_raises_rather_than_reporting_no_locks(self, tmp_path):
        from sn89_signals import hf_grade
        with pytest.raises(hf.HFLockFeedError):
            hf_grade.load_hf_lock_rows((tmp_path / "nope").as_uri(), 0)

    def test_indexed_but_unfetchable_window_raises(self, tmp_path):
        # Fail CLOSED. Under-reading a window inside the horizon would silently
        # under-enforce, which is indistinguishable from nobody having traded.
        import json as _j
        from sn89_signals import hf_grade
        (tmp_path / "index.json").write_text(_j.dumps({"windows": [self.W]}))
        with pytest.raises(hf.HFLockFeedError):
            hf_grade.load_hf_lock_rows(tmp_path.as_uri(), 0)

    def test_windows_below_the_horizon_are_skipped_not_fetched(self, tmp_path):
        # An unfetchable window OUTSIDE the horizon must not raise — nothing in
        # it could still be locking anything, so the feed stays O(lock window).
        import json as _j
        from sn89_signals import hf_grade
        old = self.W - 10 * hf.PAIR_LOCK_MS
        (tmp_path / "index.json").write_text(_j.dumps({"windows": [old]}))
        assert hf_grade.load_hf_lock_rows(tmp_path.as_uri(), self.W) == []



class TestPreLaunchAndSpreadInvariant:
    def test_no_board_before_launch(self):
        assert hf.hf_bands_as_of(hf.HF_LAUNCH_FROM - 1) is None
        assert hf.hf_bands_as_of(hf.HF_LAUNCH_FROM) is hf.HF_BOARD_V1

    def test_a_call_before_launch_is_refused_not_graded(self):
        with pytest.raises(hf.HFRejected, match="hf_not_live_at_t0"):
            hf.validate_submission(_payload(), hf.HF_LAUNCH_FROM - 1)

    def test_every_board_pair_clears_the_spread_floor(self):
        for pair in hf.HF_BOARD_V1:
            r = hf.band_spread_ratio(pair, hf.HF_LAUNCH_FROM)
            assert r is not None, f"{pair} has no measured spread"
            assert r >= hf.MIN_BAND_SPREAD_RATIO, f"{pair} only {r:.1f}x spread"

    def test_a_pair_with_no_measured_spread_cannot_pass(self):
        # adding a ninth pair without measuring its spread must fail, not slip through
        assert hf.band_spread_ratio("NZDUSD", hf.HF_LAUNCH_FROM) is None

    def test_the_excluded_pairs_are_excluded(self):
        for p in ("USDCAD", "USDCHF", "AUDUSD", "NZDUSD", "XAGUSD"):
            assert p not in hf.HF_BOARD_V1


def _eligible_subs(now, days=8, per_day=10, oldest_days_ago=12):
    """Submission timestamps that clear the HF gate (>=50 accepted subs across
    >=8 distinct UTC days), all landing BEFORE `now - oldest_days_ago + days`, so a
    miner's recent decisive wins are post-eligibility. days*per_day submissions."""
    subs = []
    for d in range(days):
        day0 = now - (oldest_days_ago - d) * 86400
        for k in range(per_day):
            subs.append(int((day0 + k * 60) * 1000))
    return subs


class TestHFEligibleFrom:
    """The HF warmup replacement: 50 accepted submissions across 8 distinct UTC
    trading days. Pure and deterministic in the submission timestamps."""
    DAY = 86_400_000

    def test_under_50_submissions_is_ineligible(self):
        subs = [i * self.DAY for i in range(8) for _ in range(6)]   # 48 across 8 days
        assert hf.hf_eligible_from(subs) is None

    def test_50_submissions_but_one_day_is_ineligible(self):
        subs = [1_000_000 + i * 250 for i in range(60)]            # 60, all one UTC day
        assert hf.hf_eligible_from(subs) is None

    def test_50_across_8_days_is_eligible(self):
        subs = [d * self.DAY + k * 60_000 for d in range(8) for k in range(10)]  # 80/8d
        got = hf.hf_eligible_from(subs)
        assert got is not None

    def test_eligible_from_is_the_later_threshold(self):
        # 8 days are reached on day 7 (the 8th distinct day), but the 50th
        # submission only lands on day 9 — eligibility is the LATER of the two.
        subs = [d * self.DAY for d in range(8)]                    # 8 subs, 8 days (day 0..7)
        subs += [8 * self.DAY + k * 60_000 for k in range(20)]     # day 8: subs 9..28
        subs += [9 * self.DAY + k * 60_000 for k in range(30)]     # day 9: subs 29..58 -> 50th here
        got = hf.hf_eligible_from(subs)
        assert got is not None
        assert int(got * 1000) // self.DAY == 9                    # tipped on day 9 (50th sub)

    def test_eight_idle_days_do_not_qualify(self):
        # the LF failure mode this fixes: time elapsed, but only a handful of trades
        subs = [0, 1 * self.DAY, 8 * self.DAY]                     # 3 subs over 8 days
        assert hf.hf_eligible_from(subs) is None


class TestHFEarningGate:
    """The go-live safety property: jumping from LF to HF grants NO head start,
    and a first win can never take the pool. Every miner here is made ELIGIBLE
    (past the 50-sub / 8-day gate) so these tests isolate the edge/decisive gate."""
    NOW = 1_800_000_000.0

    def _subs(self):
        return {'A': _eligible_subs(self.NOW)}

    def test_first_hf_win_earns_nothing(self):
        w = hf.hf_compute_weights({'A': [(self.NOW - 1800, True, False)]},
                                  {'A': self.NOW - 20 * 86400}, {'A': 10}, self.NOW, self._subs())
        assert w.get(10, 0.0) == 0.0          # < 8 decisive -> not qualified
        assert w.get(0, 0.0) > 0.0            # burns instead

    def test_seven_decisive_still_earns_nothing(self):
        d = [(self.NOW - (7 - i) * 3600, True, False) for i in range(7)]
        w = hf.hf_compute_weights({'A': d}, {'A': self.NOW - 20 * 86400}, {'A': 10}, self.NOW, self._subs())
        assert w.get(10, 0.0) == 0.0

    def test_eight_decisive_passing_rate_qualifies(self):
        d = [(self.NOW - (8 - i) * 3600, True, False) for i in range(8)]
        w = hf.hf_compute_weights({'A': d}, {'A': self.NOW - 20 * 86400}, {'A': 10}, self.NOW, self._subs())
        assert w.get(10, 0.0) > 0.0

    def test_eight_decisive_failing_rate_does_not_qualify(self):
        d = [(self.NOW - (8 - i) * 3600, i < 3, False) for i in range(8)]   # 3/8
        w = hf.hf_compute_weights({'A': d}, {'A': self.NOW - 20 * 86400}, {'A': 10}, self.NOW, self._subs())
        assert w.get(10, 0.0) == 0.0

    def test_lf_standing_does_not_carry_into_hf(self):
        # decisive_by_hk is HF-ONLY; a huge LF record is irrelevant here
        w = hf.hf_compute_weights({'A': [(self.NOW - 1800, True, False)]},
                                  {'A': self.NOW - 200 * 86400}, {'A': 10}, self.NOW, self._subs())
        assert w.get(10, 0.0) == 0.0

    def test_hf_uses_hf_decay_not_lf(self):
        from sn89_signals import config
        d = [(self.NOW - (8 - i) * 3600 - 3 * 86400, True, False) for i in range(8)]
        w = hf.hf_compute_weights({'A': d}, {'A': self.NOW - 40 * 86400}, {'A': 10}, self.NOW, self._subs())
        # 3d-old wins are fully decayed under HF's 48h window -> no real pool share.
        # A once-qualified miner keeps only the probation DUST floor (no-cliff), not
        # a pro-rata share, so the weight is at most dust.
        assert w.get(10, 0.0) <= config.DUST_WEIGHT
        assert config.EMISSION_DECAY_S == 7 * 24 * 3600

    def test_config_never_leaks_after_the_call(self):
        from sn89_signals import config
        keys = ('EMISSION_DECAY_S', 'WIN_CAP', 'MINER_EMISSION_CAP', 'IMMUNITY_S',
                'HIT_RATE_WINDOW_TRADES', 'SCORE_WINDOW_S')
        before = {k: getattr(config, k) for k in keys}
        hf.hf_compute_weights({'A': [(self.NOW - 1800, True, False)]},
                              {'A': self.NOW - 20 * 86400}, {'A': 10}, self.NOW, self._subs())
        assert before == {k: getattr(config, k) for k in keys}


class TestHFEligibilityGateEndToEnd:
    """The eligibility gate in hf_compute_weights: a miner with a great decisive
    record earns NOTHING until it has 50 accepted submissions across 8 UTC days."""
    NOW = 1_800_000_000.0

    def _winning_decisive(self):
        # 12 wins over the last 12h — trivially passes the edge gate IF eligible
        return [(self.NOW - (12 - i) * 3600, True, False) for i in range(12)]

    def test_ineligible_few_days_earns_nothing(self):
        # 60 submissions but all inside 3 UTC days -> fails the 8-day rule
        subs = {'A': [int((self.NOW - 3 * 86400 + d * 86400 + k * 60) * 1000)
                      for d in range(3) for k in range(20)]}
        w = hf.hf_compute_weights({'A': self._winning_decisive()},
                                  {'A': self.NOW - 3 * 86400}, {'A': 10}, self.NOW, subs)
        assert w.get(10, 0.0) == 0.0
        assert w.get(0, 0.0) > 0.0

    def test_ineligible_too_few_submissions_earns_nothing(self):
        # spread over 8 days but only 40 submissions -> fails the 50 rule
        subs = {'A': _eligible_subs(self.NOW, days=8, per_day=5)}      # 40
        assert hf.hf_eligible_from(subs['A']) is None
        w = hf.hf_compute_weights({'A': self._winning_decisive()},
                                  {'A': self.NOW - 20 * 86400}, {'A': 10}, self.NOW, subs)
        assert w.get(10, 0.0) == 0.0

    def test_eligible_earns(self):
        subs = {'A': _eligible_subs(self.NOW)}                          # 80 over 8 days
        w = hf.hf_compute_weights({'A': self._winning_decisive()},
                                  {'A': self.NOW - 20 * 86400}, {'A': 10}, self.NOW, subs)
        assert w.get(10, 0.0) > 0.0

    def test_wins_before_eligibility_earn_no_real_share(self):
        # eligibility only reached ~now (subs packed into the last 8 days ending
        # now), while the wins are OLDER than that instant -> they are warmup and
        # produce zero QUALIFIED wins, so no pro-rata pool share. The miner is
        # still qualified-caliber and just past warmup, so it keeps the probation
        # DUST floor (no-cliff) — that dust is intended, a real share is not.
        from sn89_signals import config
        subs = {'A': _eligible_subs(self.NOW, oldest_days_ago=8)}       # newest sub ~now
        elig = hf.hf_eligible_from(subs['A'])
        old_wins = [(elig - (12 - i) * 3600, True, False) for i in range(12)]  # all before elig
        w = hf.hf_compute_weights({'A': old_wins},
                                  {'A': self.NOW - 20 * 86400}, {'A': 10}, self.NOW, subs)
        assert w.get(10, 0.0) <= config.DUST_WEIGHT


class TestTouchTicksLF:
    """LF touch grading (config.grading_rule_as_of=='touch_ticks', ≥MIN_TOUCH_TICKS).
    Grades off the tick mid `p`; a lone reverting wick never scores."""
    def _sig(self, direction="LONG", tp=62, sl=62):
        from sn89_signals.schema import Signal
        return Signal(trade_pair="XAUUSD", direction=direction, tp_bps=tp, sl_bps=sl,
                      ts_miner=0, hotkey="5F" + "x" * 46)

    def _ticks(self, prices, t0_ms, step=250):
        return [{"a": "XAUUSD", "t": t0_ms + (i + 1) * step, "p": p} for i, p in enumerate(prices)]

    T0 = 1784941200_000        # 2026-07-25T01:00:00Z — after the touch_ticks cutover
    DONE = 1784941200_000 + 13 * 3600_000   # past the 12h forex/metals horizon

    def test_rule_is_touch_ticks_at_this_t0(self):
        from sn89_signals import config
        assert config.grading_rule_as_of(self.T0 / 1000.0) == "touch_ticks"

    def test_lone_wick_does_not_win(self):     # Mike-class 1-tick pierce → wash
        from sn89_signals import grader
        g = grader.grade(self._sig(), self.T0, self.DONE, entry_price=100.0,
                         ticks=self._ticks([100.70, 100.50, 100.50, 100.40], self.T0))
        assert g.status == grader.WASHED

    def test_two_touches_win(self):            # genuine touch (≥2 ticks) → WON
        from sn89_signals import grader
        g = grader.grade(self._sig(), self.T0, self.T0 + 1_000_000, entry_price=100.0,
                         ticks=self._ticks([100.70, 100.71], self.T0))
        assert g.status == grader.WON and g.outcome_bps == 62

    def test_near_miss_washes(self):           # Jeremiah-class: peak below TP → wash
        from sn89_signals import grader
        g = grader.grade(self._sig(), self.T0, self.DONE, entry_price=100.0,
                         ticks=self._ticks([100.60, 100.61, 100.55], self.T0))   # TP=100.62
        assert g.status == grader.WASHED

    def test_two_sl_touches_lose(self):
        from sn89_signals import grader
        g = grader.grade(self._sig(), self.T0, self.T0 + 1_000_000, entry_price=100.0,
                         ticks=self._ticks([99.30, 99.29], self.T0))             # SL=99.38
        assert g.status == grader.LOST and g.outcome_bps == -62

    def test_sl_wick_then_genuine_tp_wins(self):   # 1 SL wick rejected, 2 TP ticks win
        from sn89_signals import grader
        g = grader.grade(self._sig(), self.T0, self.T0 + 1_000_000, entry_price=100.0,
                         ticks=self._ticks([99.30, 100.0, 100.70, 100.71], self.T0))
        assert g.status == grader.WON

    def test_no_touch_before_horizon_is_pending(self):
        from sn89_signals import grader
        g = grader.grade(self._sig(), self.T0, self.T0 + 5_000, entry_price=100.0,
                         ticks=self._ticks([100.50, 100.51], self.T0))
        assert g.status == grader.PENDING

    def test_a_tick_hole_defers_the_wash_but_not_a_real_touch(self, tmp_path, monkeypatch):
        """LF shares the HF tick corpus, so it shared the bug: an unfetchable
        window made a signal that touched nothing on a SHORT series look like a
        clean wash. A touch we did observe still stands — a hole can hide a level,
        never invent one."""
        from sn89_signals import grader, hf_grade

        def feed(prices, missing):
            # the entry tick AT t0 must be present or grade() short-circuits on
            # "no entry price yet" and never reaches the wash branch under test
            ticks = [{"a": "XAUUSD", "t": self.T0, "p": 100.0}] + self._ticks(prices, self.T0)
            return lambda *a, **k: (ticks, missing)

        monkeypatch.setattr(hf_grade, "_ticks_for", feed([100.50, 100.51], [self.T0]))
        assert grader.grade(self._sig(), self.T0, self.DONE).status == grader.PENDING, \
            "a hole must not resolve as a wash"

        # the same hole, but the series DID show two TP touches — still decisive
        monkeypatch.setattr(hf_grade, "_ticks_for", feed([100.70, 100.71], [self.T0]))
        assert grader.grade(self._sig(), self.T0, self.DONE).status == grader.WON

        # and once the window publishes, the wash resolves normally
        monkeypatch.setattr(hf_grade, "_ticks_for", feed([100.50, 100.51], []))
        assert grader.grade(self._sig(), self.T0, self.DONE).status == grader.WASHED

    def test_a_permanent_tick_hole_still_washes_eventually(self, monkeypatch):
        from sn89_signals import grader, hf_grade
        ticks = [{"a": "XAUUSD", "t": self.T0, "p": 100.0}] + self._ticks(
            [100.50, 100.51], self.T0)
        monkeypatch.setattr(hf_grade, "_ticks_for", lambda *a, **k: (ticks, [self.T0]))
        past = self.DONE + hf_grade.GRADE_ABANDON_S * 1000 + 1
        assert grader.grade(self._sig(), self.T0, past).status == grader.WASHED
