"""Replayable weight derivation (single-validator audit).

Proves replay.weights_from_journal is a faithful, pure function of the journal:
a miner re-running it on the published journal reproduces the validator's weights,
and any tamper (flipping a grade, hiding a loss) changes the result — so a validator
that set weights inconsistent with its journal is caught.

    pytest tests/test_replay.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sn89_signals import config, replay, scoring  # noqa: E402
from sn89_signals.schema import Signal  # noqa: E402

NOW = 10_000_000.0
HOUR = 3600.0
OLD = NOW - 40 * 24 * HOUR   # first_seen 40d ago → warmup ended ~32d ago, so recent
                             # signals (a few days old) are post-warmup and pay emission


def _pt(hk, pair, direction="LONG"):
    return Signal(trade_pair=pair, direction=direction, tp_bps=150, sl_bps=150,
                  ts_miner=0, hotkey=hk, asset_class="crypto").canonical_bytes().decode()


def _signals(hk, pair, wins, losses, start=NOW - 5 * 24 * HOUR, step=HOUR):
    """`wins` WON then `losses` LOST decisive signals, all recent (in-window, paying)."""
    out, t = [], start
    for i in range(wins + losses):
        out.append({"commit_hex": f"{hk}-{i}", "hotkey": hk, "t0_unix": t,
                    "status": "won" if i < wins else "lost", "plaintext": _pt(hk, pair)})
        t += step
    return out


def _meta(*hks, first_seen=OLD):
    return {hk: {"first_seen_unix": first_seen, "strikes": 0} for hk in hks}


class TestReplayMatchesScoring:
    def test_qualified_miner_earns_unqualified_does_not(self):
        # A: 30/10 (75%) clears the Wilson gate; B: 20/20 (50%) does not.
        sigs = _signals("A", "BTCUSD", 30, 10) + _signals("B", "ETHUSD", 20, 20)
        uids = {"A": 1, "B": 2}
        w = replay.weights_from_journal(sigs, _meta("A", "B"), uids, NOW)
        assert w.get(1, 0) > 0.99      # A earns essentially all of it
        assert 2 not in w              # B never qualifies

    def test_reproduces_compute_weights_directly(self):
        # replay must equal calling scoring.compute_weights on the same derived state
        sigs = _signals("A", "BTCUSD", 30, 10)
        uids = {"A": 1}
        w_replay = replay.weights_from_journal(sigs, _meta("A"), uids, NOW)
        dec = [(s["t0_unix"], s["status"] == "won", False) for s in sigs]
        rw, rd, wa, wo, cp, td = scoring.score_inputs(dec, OLD, NOW)
        st = scoring.MinerState("A", 1, OLD, rep_wins=rw, rep_decisive=rd, trailing_wins=wa)
        w_direct = scoring.compute_weights([st], NOW)
        assert w_replay == w_direct

    def test_tamper_hiding_a_loss_changes_weights(self):
        # Two equal qualified miners BELOW the win cap split weight 50/50; hiding
        # one of A's losses (loss→win) lifts A's trailing wins, shifting the split,
        # so replay no longer matches the (cheated) on-chain weights.
        base = _signals("A", "BTCUSD", 10, 4) + _signals("B", "ETHUSD", 10, 4)
        uids = {"A": 1, "B": 2}
        honest = replay.weights_from_journal(base, _meta("A", "B"), uids, NOW)
        assert honest[1] == pytest.approx(honest[2])   # symmetric → 50/50
        tampered = [dict(s) for s in base]
        flipped = next(s for s in tampered if s["hotkey"] == "A" and s["status"] == "lost")
        flipped["status"] = "won"                      # validator hides a loss
        cheated = replay.weights_from_journal(tampered, _meta("A", "B"), uids, NOW)
        assert cheated[1] > cheated[2]                 # A's share rose → mismatch
        assert honest != cheated                       # the audit catches it

    def test_eliminated_is_re_derived_not_trusted(self):
        # A true sub-floor record (8/40 = 20% over 48) is eliminated by replay even
        # though meta claims no elimination — replay re-derives it from the journal.
        sigs = _signals("A", "BTCUSD", 8, 40) + _signals("B", "ETHUSD", 30, 8)
        uids = {"A": 1, "B": 2}
        w = replay.weights_from_journal(sigs, _meta("A", "B"), uids, NOW)
        assert 1 not in w                              # A eliminated → zero
        assert scoring.elimination_t0(
            [(s["t0_unix"], s["status"] == "won") for s in sigs if s["hotkey"] == "A"]
        ) is not None

    def test_struck_hotkey_excluded(self):
        sigs = _signals("A", "BTCUSD", 30, 10)
        meta = _meta("A")
        meta["A"]["strikes"] = config.STRIKE_LIMIT     # 3 strikes → zeroed
        w = replay.weights_from_journal(sigs, meta, {"A": 1}, NOW)
        assert 1 not in w

    def test_purity_no_side_effects_on_input(self):
        sigs = _signals("A", "BTCUSD", 30, 10)
        snapshot = [dict(s) for s in sigs]
        replay.weights_from_journal(sigs, _meta("A"), {"A": 1}, NOW)
        assert sigs == snapshot                        # inputs untouched (deterministic)
