"""Agent competition v2 plumbing (sn89_signals.agent_books_io) and its validator wiring."""
import hashlib
import json
import random
from types import MappingProxyType

import pytest

from sn89_signals import agent_books as ab
from sn89_signals import agent_books_io as abio
from sn89_signals import competitions, config, replay

D = ab.DAY_MS
H = 3_600_000
MON = 1793577600000          # 2026-11-02 00:00:00 UTC, a Monday
ENT = "5Hdvy1Msc9Su6U7kcJQRFnc39NNVumAcPnqFLoHqZ4Uix6g3"
BOND_CK = "5HCC5ENqaQa38uuVWxeqdpMP9dYbgebjVGtJdSShSKv4mGZH"
OTHER = "5FCN4P1KhQKzfHSj6QXwMeGjGDWkbuL8AQDtwx81F5bqxvoR"   # any valid-looking ss58
N0 = config.AGENT_ACCOUNT_MIN
UNI = {"BTCUSD": "crypto", "ETHUSD": "crypto", "SOLUSD": "crypto", "NVDAUSD": "equities",
       "EURUSD": "forex"}


def fake_verify(hk, msg, sig_hex):
    return sig_hex == hashlib.sha256(hk.encode() + msg).hexdigest()


def sign(r):
    r = dict(r)
    r["sig"] = hashlib.sha256(r["entity"].encode() + ab.receipt_signing_bytes(r)).hexdigest()
    return r


def rcpt(n, seq, t, targets, ent=ENT):
    return sign({"v": 1, "entity": ent, "account": f"{ent}_{n}", "seq": seq,
                 "t_recv_ms": t, "targets": targets})


def line(r):
    return json.dumps(r)


# ── receipts ─────────────────────────────────────────────────────────────────
def test_load_receipts_verifies_orders_and_counts_drops():
    good = [rcpt(N0 + 1, 2, MON + 2000, {"BTCUSD": 0.1}),
            rcpt(N0 + 1, 1, MON + 1000, {"BTCUSD": 0.2}),
            rcpt(N0 + 2, 1, MON + 1500, {"ETHUSD": -0.1})]
    bad_sig = dict(rcpt(N0 + 3, 1, MON, {"BTCUSD": 0.1}), sig="ab" * 32)
    tampered = dict(rcpt(N0 + 3, 2, MON, {"BTCUSD": 0.1}), targets={"BTCUSD": 0.25})
    wrong_ent = dict(rcpt(N0 + 4, 1, MON, {}), account=f"{OTHER}_{N0 + 4}")
    low_n = rcpt(7, 1, MON, {"BTCUSD": 0.1})
    lines = [line(r) for r in good] + [line(bad_sig), line(tampered), line(wrong_ent),
                                       line(low_n), "{not json", "", line(good[0])]
    rs, drops = abio.load_receipts(lines, verify=fake_verify)
    assert [(r["account"][-1], r["seq"]) for r in rs] == [("1", 1), ("2", 1), ("1", 2)]
    assert drops == {"account_not_agent": 1, "account_not_entity": 1, "bad_json": 1,
                     "bad_signature": 2, "duplicate": 1}


def test_equivocation_drops_both():
    a = rcpt(N0 + 1, 5, MON, {"BTCUSD": 0.1})
    b = rcpt(N0 + 1, 5, MON, {"BTCUSD": -0.1})
    c = rcpt(N0 + 1, 6, MON + 1, {"BTCUSD": 0.0})
    rs, drops = abio.load_receipts([line(a), line(b), line(c)], verify=fake_verify)
    assert [r["seq"] for r in rs] == [6] and drops == {"seq_conflict": 2}


def test_real_sr25519_signature():
    from bittensor_wallet import Keypair
    kp = Keypair.create_from_uri("//Alice")
    r = {"v": 1, "entity": kp.ss58_address, "account": f"{kp.ss58_address}_{N0 + 1}",
         "seq": 1, "t_recv_ms": MON, "targets": {"BTCUSD": 0.1}}
    r["sig"] = kp.sign(ab.receipt_signing_bytes(r)).hex()
    rs, drops = abio.load_receipts([line(r)])
    assert len(rs) == 1 and not drops
    forged = dict(r, targets={"BTCUSD": 0.2})
    rs, drops = abio.load_receipts([line(forged)])
    assert rs == [] and drops == {"bad_signature": 1}


# ── anchoring ────────────────────────────────────────────────────────────────
def test_anchor_v2_roundtrip_and_size():
    roots = ["11" * 32, "22" * 32, "33" * 32, "44" * 32]
    w = 1793727180000
    s = abio.encode_anchor_v2(w, 9999, 999_999, 9999, *roots)
    assert len(s.encode()) <= 128
    d = abio.decode_anchor_v2(s)
    assert d["w"] == w and d["agent_n"] == 9999
    assert abio.verify_anchor_v2(s, *roots, 9999, 999_999, 9999)
    assert not abio.verify_anchor_v2(s, roots[0], roots[1], roots[2], "55" * 32,
                                     9999, 999_999, 9999)
    assert abio.decode_anchor_v2("sn89hf:1:1:2:3:" + "00" * 32) is None


def test_quote_root_binds_bid_ask():
    t = {"a": "BTCUSD", "t": 1, "p": 100.0, "b": 99.0, "k": 101.0}
    assert abio.quote_root([t]) != abio.quote_root([dict(t, b=98.0)])


def test_filter_anchored():
    w = abio.window_start_ms(MON + 10 * H)
    r1 = rcpt(N0 + 1, 1, w + 1000, {"BTCUSD": 0.1})
    r2 = rcpt(N0 + 2, 1, w + 2000, {"BTCUSD": 0.1})
    r3 = rcpt(N0 + 1, 2, w + abio.WINDOW_MS + 5, {"BTCUSD": 0.0})
    good = {"agent_root": ab.receipts_root([r1, r2]), "agent_n": 2,
            "anchor_ts_ms": w + abio.WINDOW_MS + 60_000}
    kept, drops = abio.filter_anchored([r1, r2, r3], {w: good})
    assert kept == [r1, r2] and drops == {"unanchored": 1}
    late = dict(good, anchor_ts_ms=w + abio.WINDOW_MS + abio.AGENT_ANCHOR_MAX_LAG_MS + 1)
    assert abio.filter_anchored([r1, r2], {w: late}) == ([], {"anchored_late": 2})
    # one receipt rewritten after anchoring -> the whole window fails
    r2b = rcpt(N0 + 2, 1, w + 2000, {"BTCUSD": 0.2})
    assert abio.filter_anchored([r1, r2b], {w: good}) == ([], {"anchor_mismatch": 2})


# ── exact thinning: replay over the thin slice == replay over the full stream ─
def _world(seed, days=9, pairs=("BTCUSD", "ETHUSD", "SOLUSD", "NVDAUSD", "EURUSD")):
    rng = random.Random(seed)
    ticks = []
    for a in pairs:
        p = 100.0
        t = MON - 3 * H
        vol = 0.02 if a == "SOLUSD" else 0.004
        while t < MON + days * D:
            t += rng.choice((1000, 1000, 2500, 7000, 60_000, 400_000))
            if rng.random() < 0.002:
                t += rng.choice((2 * H, 30 * H))          # gaps, incl. over a day end
            p *= 1 + rng.gauss(0, vol)
            h = p * rng.choice((0.5, 1, 3)) / 1e4
            tk = {"a": a, "t": t, "p": p, "b": p - h, "k": p + h}
            if rng.random() < 0.03:
                tk = {"a": a, "t": t, "p": p}               # mid-only tick
            ticks.append(tk)
            if rng.random() < 0.01:                          # same-ms duplicate row
                ticks.append(dict(tk, p=p * 1.0001))
    ticks.sort(key=lambda d: (d["t"], d["a"]))
    receipts = []
    for n in range(6):
        t = MON + rng.randrange(0, 3 * H)
        seq = 0
        while t < MON + days * D:
            seq += 1
            k = rng.randrange(1, 4)
            tg = {rng.choice(pairs): round(rng.uniform(-0.25, 0.25), 3) for _ in range(k)}
            if rng.random() < 0.1:
                tg = {p: 0.0 for p in tg}
            if rng.random() < 0.03:
                tg = {"BTCUSD": 0.3}                         # refused, pair cap
            receipts.append(rcpt(N0 + n, seq, t, tg))
            t += rng.choice((997, 3000, 10 * 60_000, H, 3 * H, 9 * H))
            if rng.random() < 0.05:
                t = ab.day_start_ms(t) + D - rng.randrange(1, 4000)   # just before a day end
    return ticks, receipts


def _reader(ticks):
    win = {}
    for tk in ticks:
        win.setdefault(abio.window_start_ms(tk["t"]), []).append(json.dumps(tk))
    return lambda w: win.get(w, [])


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_thin_replay_equals_full_replay(seed):
    ticks, receipts = _world(seed)
    end = MON + 7 * D
    rs = [r for r in receipts if r["t_recv_ms"] < end]
    thin, missing = abio.load_thin_range(MON, end, rs, UNI, _reader(ticks))
    assert not missing
    assert len(thin) < len(ticks) / 3
    got = abio.replay_all(rs, abio.PairIndex(thin), UNI, start_ms=MON, end_ms=end)
    for acct in sorted(got):
        full = ab.replay_book(acct, rs, ticks, UNI, start_ms=MON, end_ms=end)
        assert got[acct] == full, acct
    assert any(got[a]["fills"] for a in got)


def test_thin_replay_exact_through_elimination():
    ticks, receipts = _world(4)
    sol = [rcpt(N0 + 50, i + 1, MON + i * 6 * H + 1, {"SOLUSD": 0.25 if i % 2 else -0.25})
           for i in range(28)]
    end = MON + 7 * D
    rs = [r for r in receipts + sol if r["t_recv_ms"] < end]
    thin, _ = abio.load_thin_range(MON, end, rs, UNI, _reader(ticks))
    got = abio.replay_all(rs, abio.PairIndex(thin), UNI, start_ms=MON, end_ms=end)
    acct = f"{ENT}_{N0 + 50}"
    full = ab.replay_book(acct, rs, ticks, UNI, start_ms=MON, end_ms=end)
    assert full["eliminated_at_ms"] is not None
    assert got[acct] == full


def test_thin_cache_roundtrip_and_invalidation(tmp_path):
    ticks, receipts = _world(11)
    end = MON + 7 * D
    rs = [r for r in receipts if r["t_recv_ms"] < end]
    rd = _reader(ticks)
    a, _ = abio.load_thin_range(MON, end, rs, UNI, rd, cache_dir=str(tmp_path),
                                final_before_ms=end)
    b, _ = abio.load_thin_range(MON, end, rs, UNI, lambda w: None, cache_dir=str(tmp_path),
                                final_before_ms=end)       # served entirely from cache
    assert a == b
    # a late receipt changes the digest of its day -> recomputed, not served stale
    extra = rcpt(N0 + 9, 1, MON + 2 * D + 5 * H, {"BTCUSD": 0.2})
    c, miss = abio.load_thin_range(MON, end, rs + [extra], UNI, lambda w: None,
                                   cache_dir=str(tmp_path), final_before_ms=end)
    assert miss  # the days it had to recompute had no shards


def test_missing_windows_reported():
    ticks, receipts = _world(5, days=1)
    rd = _reader(ticks)
    gone = abio.window_start_ms(MON + 5 * H)
    _, missing = abio.load_thin_range(MON, MON + D, receipts, UNI,
                                      lambda w: None if w == gone else rd(w))
    assert missing == [gone]


# ── registry and bond ────────────────────────────────────────────────────────
def test_our_entity_grandfathered_without_commitment():
    reg = abio.registry_as_of({}, 123)
    assert reg == {ENT: BOND_CK}


def test_registry_journal_sticky_and_as_of():
    j = abio.merge_registry({}, {OTHER: {"bond_coldkey": BOND_CK, "commit_block": 100}})
    j = abio.merge_registry(j, {OTHER: {"bond_coldkey": BOND_CK, "commit_block": 150}})
    assert len(j[OTHER]) == 1                      # same coldkey re-seen: no new row
    j = abio.merge_registry(j, {OTHER: {"bond_coldkey": ENT, "commit_block": 200}})
    assert abio.registry_as_of(j, 99, grandfathered={}) == {}
    assert abio.registry_as_of(j, 150, grandfathered={}) == {OTHER: BOND_CK}
    assert abio.registry_as_of(j, 200, grandfathered={}) == {OTHER: ENT}
    # observed later but gone from chain now: still registered (sticky)
    assert abio.registry_as_of(abio.merge_registry(j, {}), 300, grandfathered={})[OTHER] == ENT


def _results(paid_by_acct):
    out = {}
    for a, paid in paid_by_acct.items():
        out[a] = {"weeks": [{"week_start_ms": MON, "gain": abs(paid), "active": True,
                             "eliminated": False, "paid": max(paid, 0.0)}],
                  "eliminated_at_ms": None}
    return out


def test_week_bond_gate_and_shares():
    a1, a2, b1 = f"{ENT}_{N0 + 1}", f"{ENT}_{N0 + 2}", f"{OTHER}_{N0 + 1}"
    res = _results({a1: 0.03, a2: 0.01, b1: 0.04})
    reg = {ENT: BOND_CK, OTHER: BOND_CK}
    doc = abio.build_week(res, MON, reg, {ENT: 2380.39, OTHER: 10_000.0}, [])
    assert doc["accounts"][a1]["share"] == 0 and doc["accounts"][a2]["share"] == 0
    assert doc["accounts"][b1]["share"] == 1.0
    assert doc["entities"][ENT] == {"registered": True, "bond_coldkey": BOND_CK,
                                    "bond": 2380.39, "eligible": False}
    doc = abio.build_week(res, MON, reg, {ENT: 10_000.0, OTHER: 10_000.0}, [])
    assert doc["accounts"][a1]["share"] == pytest.approx(0.375)
    assert doc["accounts"][b1]["share"] == pytest.approx(0.5)
    # registered nowhere -> zero even with a bond
    doc = abio.build_week(res, MON, {ENT: BOND_CK}, {ENT: 1e5, OTHER: 1e5}, [])
    assert doc["accounts"][b1]["share"] == 0 and not doc["entities"][OTHER]["eligible"]


def _freeze(lines, ticks, bonds):
    rs, _ = abio.load_receipts(lines, verify=fake_verify)
    end = MON + 7 * D
    rs = [r for r in rs if r["t_recv_ms"] < end]
    thin, missing = abio.load_thin_range(MON, end, rs, UNI, _reader(ticks))
    res = abio.replay_all(rs, abio.PairIndex(thin), UNI, start_ms=MON, end_ms=end)
    doc = abio.build_week(res, MON, abio.registry_as_of({}, 0), bonds, rs,
                          extra={"epoch": MON, "missing_windows": len(missing)})
    return abio.dumps_week(doc)


def test_frozen_week_is_byte_deterministic():
    ticks, receipts = _world(7)
    lines = [line(r) for r in receipts]
    a = _freeze(lines, ticks, {ENT: 12_345.0})
    shuffled = list(lines)
    random.Random(3).shuffle(shuffled)
    b = _freeze(shuffled + lines[:5], ticks, {ENT: 12_345.0})   # order + duplicates
    assert a == b
    doc = json.loads(a)
    assert set(doc) >= {"week_start", "rules_version", "accounts", "entities",
                        "receipts_root"}
    assert abs(sum(r["share"] for r in doc["accounts"].values()) - 1.0) < 1e-12 or \
        all(r["share"] == 0 for r in doc["accounts"].values())


# ── validator side ───────────────────────────────────────────────────────────
def test_pay_week_rolls_over_after_freeze_delay():
    nxt = MON + ab.WEEK_MS
    delay = config.AGENT_BOOKS_PAY_DELAY_S
    assert abio.pay_week_start_ms(nxt / 1000 + delay - 1) == MON - ab.WEEK_MS
    assert abio.pay_week_start_ms(nxt / 1000 + delay) == MON
    # the freeze is possible 2 h before anyone pays from it
    fz = config.AGENT_BOOKS_FREEZE_DELAY_S
    assert fz < delay
    assert abio.freezable_week_start_ms(nxt + fz * 1000) == MON
    assert abio.freezable_week_start_ms(nxt + fz * 1000 - 1) == MON - ab.WEEK_MS


def test_flag_off_by_default_changes_nothing(tmp_path, monkeypatch):
    assert config.AGENT_BOOKS_FROM_UNIX == 0
    assert not config.agent_books_active(2**40)
    # even with a frozen file sitting there, the flag-off path reads nothing
    doc = abio.build_week(_results({f"{ENT}_{N0}": 0.1}), MON, {ENT: BOND_CK},
                          {ENT: 1e5}, [])
    (tmp_path / "agent-books-week-2026-11-02.json").write_bytes(abio.dumps_week(doc))
    assert abio.cycle_shares(MON / 1000 + ab.WEEK_MS / 1000 + 7 * 3600,
                             out_dir=str(tmp_path)) is None
    # the validator's vectors are the same, so the commit is identical
    shares = config.comp_weights_as_of(1793836800)
    vec = {"lf": {1: 0.5, 2: 0.5}, "agent": {3: 1.0}, "markets": {4: 1.0}}
    assert competitions.combine(dict(vec), shares) == competitions.combine(
        dict(vec, agentbook=None), shares)


def test_flag_on_pays_the_frozen_week(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_BOOKS_FROM_UNIX", MON // 1000)   # intake opens MON
    assert abio.epoch_ms() == MON
    # first week not paid yet: no file is even looked for, the share burns
    assert abio.cycle_shares(MON / 1000 + 3 * 86400, out_dir=str(tmp_path)) is None
    a1, a2 = f"{ENT}_{N0 + 1}", f"{ENT}_{N0 + 2}"
    doc = abio.build_week(_results({a1: 0.03, a2: 0.01}), MON, {ENT: BOND_CK},
                          {ENT: 1e5}, [])
    (tmp_path / "agent-books-week-2026-11-02.json").write_bytes(abio.dumps_week(doc))
    now = (MON + ab.WEEK_MS) // 1000 + config.AGENT_BOOKS_PAY_DELAY_S + 60
    sh = abio.cycle_shares(now, out_dir=str(tmp_path))
    assert sh == {a1: 0.75, a2: 0.25}
    assert abio.agentbook_vector(sh, {ENT: 39}) == {39: 1.0}
    assert abio.agentbook_vector(sh, {}) == {config.BURN_UID: 1.0}
    assert abio.agentbook_vector({}, {ENT: 39}) is None
    # no share in COMP_WEIGHTS yet -> combine ignores the vector entirely
    shares = config.comp_weights_as_of(1793836800)
    base = {"lf": {1: 1.0}, "agent": {3: 1.0}, "markets": {4: 1.0}}
    assert competitions.combine(dict(base, agentbook={39: 1.0}), shares) == \
        competitions.combine(base, shares)
    # a week with no file -> None, the share burns
    assert abio.cycle_shares(now + ab.WEEK_MS / 1000, out_dir=str(tmp_path)) is None


def test_entity_split_feeds_agentbook_accounts(monkeypatch):
    a1, a2 = f"{ENT}_{N0 + 1}", f"{ENT}_{N0 + 2}"
    sh = {a1: 0.75, a2: 0.25}
    shares = {"lf": 0.375, "agentbook": 0.25, "reserve": 0.125, "markets": 0.25}
    split = replay.entity_split(lambda m: {}, lambda m: {}, None, shares, {ENT: 39},
                                set(sh), agentbook_fn=lambda m: abio.agentbook_vector(sh, m))
    assert split[ENT]["accounts"] == pytest.approx({a1: 0.25 * 0.75, a2: 0.25 * 0.25})
    # without agentbook_fn the split is exactly what it was
    assert replay.entity_split(lambda m: {}, lambda m: {}, None, shares, {ENT: 39}, set(sh)
                               )[ENT]["accounts"] == {}


def test_universe_union_keeps_fx_for_refusal():
    u = abio.book_universe_through(2**40)
    assert u.get("BTCUSD") == "crypto" and u.get("EURUSD") == "forex"
    assert "forex" in ab.RULES_V1["excluded_classes"]


def test_index_and_url_loader():
    base = "https://partner.infinitequant.app/sn89/agent/books"
    a, b = rcpt(N0 + 1, 1, MON, {"BTCUSD": 0.1}), rcpt(N0 + 2, 1, MON + 5, {"ETHUSD": 0.1})
    pages = {f"{base}/index.json": json.dumps({"books": [f"{ENT}_{N0 + 1}", N0 + 2]}),
             f"{base}/{N0 + 1}.jsonl": line(a) + "\n",
             f"{base}/{N0 + 2}.jsonl": line(b) + "\n"}
    rs, drops = abio.load_receipts(abio.iter_url_lines([f"{base}/index.json"], pages.get),
                                   verify=fake_verify)
    assert rs == [a, b] and not drops
    for idx in ([f"{N0 + 1}.jsonl"], {f"{ENT}_{N0 + 1}": {"n": N0 + 1}},
                {"accounts": [{"n": N0 + 1}]}):
        assert abio.index_urls(f"{base}/index.json", idx) == [f"{base}/{N0 + 1}.jsonl"]


def test_live_state_shape_agreed_with_intake():
    ticks, receipts = _world(2, days=2)
    rs = [r for r in receipts if r["t_recv_ms"] < MON + 2 * D]
    end = MON + 2 * D - 1
    thin, _ = abio.load_thin_range(MON, end, rs, UNI, _reader(ticks))
    doc = abio.build_live(abio.replay_all(rs, abio.PairIndex(thin), UNI, start_ms=MON,
                                          end_ms=end), end)
    assert doc["as_of_ms"] == end and doc["books"]
    for acct, bk in doc["books"].items():
        assert {"positions", "equity", "balance", "hwm", "eliminated_at_ms",
                "week_active_buckets"} <= set(bk)
        assert all(isinstance(v, float) for v in bk["positions"].values())
        assert sum(abs(v) for v in bk["positions"].values()) < 1.2
    json.dumps(doc, allow_nan=False)


def test_publish_weeks_copies_with_sha_and_index(tmp_path):
    import importlib.util
    import pathlib
    p = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "agent-books" / "publish_weeks.py"
    spec = importlib.util.spec_from_file_location("publish_weeks", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    data = abio.dumps_week(abio.build_week(_results({f"{ENT}_{N0}": 0.1}), MON,
                                           {ENT: BOND_CK}, {ENT: 1e5}, []))
    (src / "agent-books-week-2026-11-02.json").write_bytes(data)
    (src / "agent-books-entities.json").write_text("{}")
    assert m.main(str(src), str(dst)) == 0 and m.main(str(src), str(dst)) == 0
    assert (dst / "agent-books-week-2026-11-02.json").read_bytes() == data
    sha = hashlib.sha256(data).hexdigest()
    assert (dst / "agent-books-week-2026-11-02.json.sha256").read_text().strip() == sha
    idx = json.loads((dst / "index.json").read_text())
    assert idx == {"weeks": [{"week": "2026-11-02", "file": "agent-books-week-2026-11-02.json",
                              "sha256": sha}]}
