"""Signals entity accounts (config.SIGNALS_ENTITY_*): one UID, many scored accounts."""
import pytest

from sn89_signals import config, entity, hf

DAY = 86400.0
NOW = 1_800_000_000.0
ENT = "5" + "E" * 47          # 48 chars, matches the ss58 shape the regex accepts
OTHER = "5" + "F" * 47


def _subs(now, n=120, days=20):
    out = []
    for i in range(n):
        t = now - days * DAY + i * (days * DAY / n)
        out.append((int(t * 1000), "BTCUSD", "SHORT" if i % 2 else "LONG", 1800))
    return out


def _wins(now, n=12):
    return [(now - 3 * DAY + i * 3600, True, False) for i in range(n)]


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(config, "SIGNALS_ENTITY_FROM", 1)
    monkeypatch.setattr(config, "SIGNALS_ENTITY_HOTKEYS", frozenset({ENT}))
    monkeypatch.setattr(hf, "integrity_ok", lambda hk, now: True)


@pytest.fixture
def disarmed(monkeypatch):
    monkeypatch.setattr(config, "SIGNALS_ENTITY_FROM", 0)
    monkeypatch.setattr(config, "SIGNALS_ENTITY_HOTKEYS", frozenset({ENT}))
    monkeypatch.setattr(hf, "integrity_ok", lambda hk, now: True)


def test_plain_call_scores_under_signer(armed):
    assert entity.scoring_key(OTHER, {"trade_pair": "BTCUSD"}, NOW) == OTHER
    entity.check_claim(OTHER, {"trade_pair": "BTCUSD"}, NOW)


def test_entity_call_scores_under_account(armed):
    p = {"account": f"{ENT}_7"}
    entity.check_claim(ENT, p, NOW)
    assert entity.scoring_key(ENT, p, NOW) == f"{ENT}_7"


@pytest.mark.parametrize("signer,account,reason", [
    (OTHER, f"{ENT}_1", "account_not_signer"),      # cannot call for someone else's account
    (OTHER, f"{OTHER}_1", "not_an_entity"),         # not on the allow-list
    (ENT, f"{ENT}", "bad_account"),                 # bare hotkey is not an account
    (ENT, "nonsense_1", "bad_account"),
])
def test_bad_claims_rejected_and_score_under_signer(armed, signer, account, reason):
    p = {"account": account}
    with pytest.raises(entity.EntityRejected, match=reason):
        entity.check_claim(signer, p, NOW)
    assert entity.scoring_key(signer, p, NOW) == signer


def test_not_armed_rejects_claim_and_ignores_it(disarmed):
    p = {"account": f"{ENT}_7"}
    with pytest.raises(entity.EntityRejected, match="entity_not_armed"):
        entity.check_claim(ENT, p, NOW)
    assert entity.scoring_key(ENT, p, NOW) == ENT


def test_arm_is_as_of_the_call(monkeypatch, armed):
    monkeypatch.setattr(config, "SIGNALS_ENTITY_FROM", int(NOW))
    p = {"account": f"{ENT}_7"}
    assert entity.scoring_key(ENT, p, NOW - 1) == ENT
    assert entity.scoring_key(ENT, p, NOW) == f"{ENT}_7"


def test_with_accounts_maps_to_entity_uid(armed):
    m = entity.with_accounts({ENT: 5}, {f"{ENT}_1", f"{ENT}_2", f"{OTHER}_1", "X"})
    assert m[f"{ENT}_1"] == 5 and m[f"{ENT}_2"] == 5
    assert f"{OTHER}_1" not in m and "X" not in m


def test_unregistered_entity_resolves_to_nothing(armed):
    assert entity.with_accounts({}, {f"{ENT}_1"}) == {}


def test_entity_weight_is_sum_of_its_accounts(armed):
    a1, a2 = f"{ENT}_1", f"{ENT}_2"
    fs = lambda d: {k: NOW - 30 * DAY for k in d}
    # Same two records as separate UIDs, plus a third independent miner.
    dec_sep = {"A": _wins(NOW), "B": _wins(NOW, 10), "C": _wins(NOW)}
    subs_sep = {"A": _subs(NOW), "B": _subs(NOW), "C": _subs(NOW)}
    sep = hf.hf_compute_weights(dec_sep, fs(dec_sep), {"A": 10, "B": 11, "C": 12}, NOW, subs_sep)
    dec_ent = {a1: _wins(NOW), a2: _wins(NOW, 10), "C": _wins(NOW)}
    subs_ent = {a1: _subs(NOW), a2: _subs(NOW), "C": _subs(NOW)}
    ent = hf.hf_compute_weights(dec_ent, fs(dec_ent), {ENT: 5, "C": 12}, NOW, subs_ent)
    assert sep[10] > 0 and sep[11] > 0
    assert ent[5] == pytest.approx(sep[10] + sep[11])
    assert ent[12] == pytest.approx(sep[12])


def test_disarmed_vector_is_unchanged(disarmed):
    # Before the arm every call scores under its signer, so an entity hotkey's
    # calls are one miner, exactly as today.
    calls = [{"account": f"{ENT}_{n}"} for n in range(3)] + [{}]
    assert {entity.scoring_key(ENT, p, NOW) for p in calls} == {ENT}


# ── LF: batch commitment, expansion, replay ──────────────────────────────────
from sn89_signals import replay  # noqa: E402
from sn89_signals.schema import Signal  # noqa: E402

HOUR = 3600.0
OLD = NOW - 40 * DAY


def _lf_pt(hk, pair="BTCUSD", direction="LONG", nonce=None):
    s = Signal(trade_pair=pair, direction=direction, tp_bps=150, sl_bps=150,
               ts_miner=0, hotkey=hk, asset_class="crypto")
    if nonce:
        s.nonce = nonce
    return s.canonical_bytes()


def test_batch_commitment_roundtrip():
    pytest.importorskip("bittensor")
    from sn89_signals import chain
    root = "ab" * 32
    data = chain.encode_batch(root, 123, "https://x/y")
    assert len(data) <= 128
    d = chain._decode_any(data)
    assert d["kind"] == "batch" and d["commit"] == root and d["round"] == 123
    assert chain.decode_commitment(data) is None       # old decoders ignore it


def _batch(pts):
    items = [{"owner_pk": config.OWNER_PK_HEX, "ct": p.hex()} for p in pts]
    blob = {"kind": entity.BATCH_KIND, "owner_pk": config.OWNER_PK_HEX, "items": items}
    return blob, (lambda it: bytes.fromhex(it["ct"]))


def test_expand_batch_opens_every_account_call(armed, monkeypatch):
    monkeypatch.setattr(config, "allowed_assets_as_of",
                        lambda t: {"BTCUSD": {"asset_class": "crypto", "tp_bps": 150, "sl_bps": 150}})
    pts = [_lf_pt(f"{ENT}_1"), _lf_pt(f"{ENT}_2", direction="SHORT")]
    blob, dec = _batch(pts)
    out = entity.expand_batch(blob, entity.batch_root(pts), ENT, NOW, dec)
    assert [Signal.from_bytes(p).hotkey for _, p, _ in out] == [f"{ENT}_1", f"{ENT}_2"]
    assert [c for c, _, _ in out] == [entity.batch_leaf(p).hex() for p in pts]


@pytest.mark.parametrize("mutate,reason", [
    (lambda pts: ([_lf_pt(f"{OTHER}_1")], None), "batch_invalid_item"),   # not this entity's account
    (lambda pts: (pts, "00" * 32), "batch_root_mismatch"),                # root does not match
    (lambda pts: (pts + pts[:1], None), "batch_duplicate_item"),
])
def test_expand_batch_is_all_or_nothing(armed, monkeypatch, mutate, reason):
    monkeypatch.setattr(config, "allowed_assets_as_of",
                        lambda t: {"BTCUSD": {"asset_class": "crypto", "tp_bps": 150, "sl_bps": 150}})
    pts, root = mutate([_lf_pt(f"{ENT}_1")])
    blob, dec = _batch(pts)
    with pytest.raises(entity.EntityRejected, match=reason):
        entity.expand_batch(blob, root or entity.batch_root(pts), ENT, NOW, dec)


def _lf_signals(hk, pair, wins, losses, start=NOW - 5 * DAY):
    out, t = [], start
    for i in range(wins + losses):
        out.append({"commit_hex": f"{hk}-{i}", "hotkey": hk, "t0_unix": t,
                    "status": "won" if i < wins else "lost",
                    "plaintext": _lf_pt(hk, pair).decode()})
        t += HOUR
    return out


def _meta(*hks):
    return {hk: {"first_seen_unix": OLD, "strikes": 0} for hk in hks}


def test_lf_entity_weight_is_sum_of_its_accounts(armed, monkeypatch):
    monkeypatch.setattr(config, "MINER_EMISSION_CAP", 1.0)
    a1, a2 = f"{ENT}_1", f"{ENT}_2"
    sep = replay.weights_from_journal(
        _lf_signals("A", "BTCUSD", 30, 10) + _lf_signals("B", "ETHUSD", 20, 8)
        + _lf_signals("C", "SOLUSD", 30, 10), _meta("A", "B", "C"),
        {"A": 1, "B": 2, "C": 3}, NOW)
    ent = replay.weights_from_journal(
        _lf_signals(a1, "BTCUSD", 30, 10) + _lf_signals(a2, "ETHUSD", 20, 8)
        + _lf_signals("C", "SOLUSD", 30, 10), _meta(a1, a2, "C"),
        {ENT: 5, "C": 3}, NOW)
    assert sep[1] > 0 and sep[2] > 0
    assert ent[5] == pytest.approx(sep[1] + sep[2])
    assert ent[3] == pytest.approx(sep[3])


def test_flagged_account_does_not_zero_the_entity(armed, monkeypatch):
    monkeypatch.setattr(config, "MINER_EMISSION_CAP", 1.0)
    monkeypatch.setattr(config, "COPY_ZERO_WEIGHT", True)
    a1, a2 = f"{ENT}_1", f"{ENT}_2"
    monkeypatch.setattr(replay.scoring, "flagged_copier_hotkeys", lambda reports: {a2})
    w = replay.weights_from_journal(
        _lf_signals(a1, "BTCUSD", 30, 10) + _lf_signals(a2, "ETHUSD", 30, 10),
        _meta(a1, a2), {ENT: 5}, NOW)
    alone = replay.weights_from_journal(
        _lf_signals(a1, "BTCUSD", 30, 10), _meta(a1), {ENT: 5}, NOW)
    assert w.get(5, 0) > 0
    assert w[5] == pytest.approx(alone[5])


# ── referral with accounts ───────────────────────────────────────────────────
def test_account_commitments_roundtrip():
    pytest.importorskip("bittensor")
    from sn89_signals import chain
    real = "5Hdvy1Msc9Su6U7kcJQRFnc39NNVumAcPnqFLoHqZ4Uix6g3"
    assert chain._decode_any(chain.encode_account_open(42)) == {"n": 42, "kind": "account_open"}
    d = chain._decode_any(chain.encode_account_referral(3, f"{real}_9"))
    assert d == {"n": 3, "recruit": f"{real}_9", "kind": "account_referral"}
    assert chain._decode_any(chain.encode_referral(f"{real}_9"))["recruit"] == f"{real}_9"
    for data in (chain.encode_account_open(1), chain.encode_account_referral(1, real)):
        assert len(data) <= 128


def test_account_pairs_validate_like_hotkeys():
    from sn89_signals import scoring
    lead = config.REFERRAL_MIN_LEAD_BLOCKS
    refs = [
        {"recruiter_hk": f"{ENT}_1", "recruit_hk": f"{ENT}_2",
         "commit_block": 100, "recruit_reg_block": 100 + lead},          # account → account
        {"recruiter_hk": OTHER, "recruit_hk": f"{ENT}_3",
         "commit_block": 100, "recruit_reg_block": 100 + lead - 1},      # account opened too early
    ]
    assert scoring.valid_referral_pairs(refs) == [(f"{ENT}_1", f"{ENT}_2")]


def test_account_recruiter_paid_through_entity_uid(armed, monkeypatch):
    monkeypatch.setattr(config, "MINER_EMISSION_CAP", 1.0)
    rec, recruit = f"{ENT}_1", "R"
    refs = [{"recruiter_hk": rec, "recruit_hk": recruit, "commit_block": 1,
             "recruit_reg_block": 1 + config.REFERRAL_MIN_LEAD_BLOCKS}]
    sigs = _lf_signals(recruit, "BTCUSD", 30, 10) + _lf_signals(rec, "ETHUSD", 30, 10)
    w = replay.referrer_weights_from_journal(
        sigs, _meta(rec, recruit), {ENT: 5, recruit: 7}, NOW, referrals=refs,
        extra_tallies={"hf": {}, "closers": {}})
    assert w.get(5, 0) > 0


def test_entity_split_measures_each_account(armed, monkeypatch):
    monkeypatch.setattr(config, "MINER_EMISSION_CAP", 1.0)
    a1, a2 = f"{ENT}_1", f"{ENT}_2"
    sigs = (_lf_signals(a1, "BTCUSD", 30, 10) + _lf_signals(a2, "ETHUSD", 20, 8)
            + _lf_signals("C", "SOLUSD", 30, 10))
    meta = _meta(a1, a2, "C")
    uids = {ENT: 5, "C": 3}
    shares = {"lf": 0.5, "hf": 0.0, "markets": 0.5}
    split = replay.entity_split(
        lambda m: replay.weights_from_journal(sigs, meta, m, NOW), lambda m: {},
        {5: 1.0, 3: 1.0}, shares, uids, {a1, a2})[ENT]
    lf = replay.weights_from_journal(sigs, meta, uids, NOW)
    lf_total = sum(lf.values())
    # the two accounts' parts add up to the entity's LF part of the combined vector
    assert sum(split["accounts"].values()) == pytest.approx(0.5 * lf[5] / lf_total)
    assert split["accounts"][a1] > split["accounts"][a2] > 0
    assert split["markets"] == pytest.approx(0.25)
    assert 0 < split["markets_frac"] < 1


def test_commit_slot_spaces_writers(tmp_path):
    import time
    lock = str(tmp_path / "c.lock")
    t0 = time.time()
    with entity.commit_slot(lock, min_gap_s=0.3):
        pass
    with entity.commit_slot(lock, min_gap_s=0.3):
        pass
    assert time.time() - t0 >= 0.3
