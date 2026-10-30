"""Agent competition (config.AGENT_*): hosted IQ Agent accounts score in `agent`, never in `lf`."""
import pytest

from sn89_signals import config, entity, hf, replay, scoring
from sn89_signals.schema import Signal

DAY, HOUR = 86400.0, 3600.0
NOW = 1_800_000_000.0
OLD = NOW - 40 * DAY
ENT = "5" + "E" * 47
AG = f"{ENT}_{config.AGENT_ACCOUNT_MIN + 1}"
SIG_ACCT = f"{ENT}_1"
BOARD = {"BTCUSD": {"asset_class": "crypto", "tp_bps": 150, "sl_bps": 150},
         "ETHUSD": {"asset_class": "crypto", "tp_bps": 150, "sl_bps": 150},
         "SOLUSD": {"asset_class": "crypto", "tp_bps": 150, "sl_bps": 150},
         "EURUSD": {"asset_class": "forex", "tp_bps": 35, "sl_bps": 35}}


@pytest.fixture(autouse=True)
def armed(monkeypatch):
    monkeypatch.setattr(config, "SIGNALS_ENTITY_FROM", 1)
    monkeypatch.setattr(config, "SIGNALS_ENTITY_HOTKEYS", frozenset({ENT}))
    monkeypatch.setattr(config, "MINER_EMISSION_CAP", 1.0)
    monkeypatch.setattr(config, "allowed_assets_as_of", lambda t: BOARD)
    monkeypatch.setattr(hf, "integrity_ok", lambda hk, now: True)


def _pt(hk, pair):
    return Signal(trade_pair=pair, direction="LONG", tp_bps=150, sl_bps=150, ts_miner=0,
                  hotkey=hk, asset_class=BOARD[pair]["asset_class"]).canonical_bytes().decode()


def _rows(hk, pair, wins, losses, start=NOW - 5 * DAY):
    return [{"commit_hex": f"{hk}-{pair}-{i}", "hotkey": hk, "t0_unix": start + i * HOUR,
             "status": "won" if i < wins else "lost", "plaintext": _pt(hk, pair)}
            for i in range(wins + losses)]


def _meta(*hks):
    return {hk: {"first_seen_unix": OLD, "strikes": 0} for hk in hks}


def _agent(sig, meta, uids):
    with scoring.qualify_gate(config.AGENT_QUALIFY_LB_FLOOR, config.AGENT_QUALIFY_MIN_DECISIVE):
        return replay.weights_from_journal(sig, meta, uids, NOW, scope="agent")


def _earning(w):
    return {u for u, x in w.items() if x > 0 and u != config.BURN_UID}


def test_agent_account_numbering():
    assert entity.is_agent_account(AG)
    assert not entity.is_agent_account(SIG_ACCT)
    assert not entity.is_agent_account("5" + "F" * 47 + f"_{config.AGENT_ACCOUNT_MIN + 1}")
    assert not entity.is_agent_account("C")


def test_agent_accounts_score_in_agent_and_never_in_lf():
    sig = _rows(AG, "BTCUSD", 40, 10) + _rows("C", "SOLUSD", 30, 10)
    meta, uids = _meta(AG, "C"), {ENT: 5, "C": 3}
    lf = replay.weights_from_journal(sig, meta, uids, NOW)
    ag = _agent(sig, meta, uids)
    assert 3 in _earning(lf) and 5 not in _earning(lf)
    assert _earning(ag) == {5}


def test_signals_entity_accounts_stay_in_lf():
    sig = _rows(SIG_ACCT, "BTCUSD", 40, 10)
    meta, uids = _meta(SIG_ACCT), {ENT: 5}
    assert _earning(replay.weights_from_journal(sig, meta, uids, NOW)) == {5}
    assert _earning(_agent(sig, meta, uids)) == set()


def test_agent_gate_is_stricter_than_lf():
    # 10 of 12: clears LF (n >= 8, LB >= 0.50) but is under the agent sample floor of 40.
    sig = _rows(AG, "BTCUSD", 10, 2)
    assert _earning(_agent(sig, _meta(AG), {ENT: 5})) == set()
    with scoring.qualify_gate(config.QUALIFY_LB_FLOOR, config.QUALIFY_MIN_DECISIVE):
        assert _earning(replay.weights_from_journal(sig, _meta(AG), {ENT: 5}, NOW,
                                                    scope="agent")) == {5}


def test_coin_flip_does_not_qualify_for_agent():
    sig = _rows(AG, "BTCUSD", 26, 24)                  # 52% over 50 decisive
    assert _earning(_agent(sig, _meta(AG), {ENT: 5})) == set()


def test_fx_calls_score_nowhere_in_agent():
    sig = _rows(AG, "EURUSD", 40, 10)
    assert _earning(_agent(sig, _meta(AG), {ENT: 5})) == set()


def test_calls_before_the_opening_do_not_count(monkeypatch):
    monkeypatch.setattr(config, "AGENT_COMP_FROM_UNIX", int(NOW))
    sig = _rows(AG, "BTCUSD", 40, 10)
    assert _earning(_agent(sig, _meta(AG), {ENT: 5})) == set()


def test_gate_override_restores():
    with scoring.qualify_gate(0.9, 99):
        assert not scoring.is_qualified(50, 60)
    assert scoring._GATE is None
    assert scoring.is_qualified(30, 40)


def test_agent_unpaid_until_its_share():
    assert config.agent_comp_active(config.AGENT_COMP_FROM_UNIX)
    assert config.comp_weights_as_of(config.HF_END_FROM_UNIX - 1).get("agent", 0) == 0
    assert config.comp_weights_as_of(config.HF_END_FROM_UNIX)["agent"] == pytest.approx(0.25)


def test_bad_scope_raises():
    with pytest.raises(ValueError):
        replay.weights_from_journal([], {}, {}, NOW, scope="hf")


def test_entity_split_pays_agent_accounts_from_the_agent_share():
    sig = _rows(AG, "BTCUSD", 40, 10) + _rows(SIG_ACCT, "ETHUSD", 30, 10)
    meta, uids = _meta(AG, SIG_ACCT), {ENT: 5}
    shares = {"lf": 0.375, "agent": 0.25, "reserve": 0.125, "markets": 0.25}
    lf = lambda m: replay.weights_from_journal(sig, meta, m, NOW)          # noqa: E731
    ag = lambda m: _agent(sig, meta, m)                                     # noqa: E731
    split = replay.entity_split(lf, lambda m: {}, None, shares, uids, {AG, SIG_ACCT}, agent_fn=ag)[ENT]
    assert set(split["accounts"]) == {AG, SIG_ACCT}
    assert split["accounts"][AG] == pytest.approx(0.25 * ag({**uids, AG: -1, SIG_ACCT: -2})[-1])
    assert split["accounts"][SIG_ACCT] == pytest.approx(0.375 * lf({**uids, AG: -1, SIG_ACCT: -2})[-2])


def test_entity_split_unpaid_agent_before_its_share():
    sig = _rows(AG, "BTCUSD", 40, 10)
    meta, uids = _meta(AG), {ENT: 5}
    shares = config.comp_weights_as_of(config.HF_END_FROM_UNIX - 1)
    split = replay.entity_split(lambda m: replay.weights_from_journal(sig, meta, m, NOW),
                                lambda m: {}, None, shares, uids, {AG},
                                agent_fn=lambda m: _agent(sig, meta, m))[ENT]
    assert split["accounts"] == {}
