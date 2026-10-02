"""HF probation for diversity-blocked miners (config.HF_GATED_PROBATION_FROM).

5HaxM3Fj (UID 109) qualified on HF, was paid one tempo on 2026-09-28, then failed
the both-sides gate and was dropped before compute_weights, so the 30-day probation
floor never saw it. It sat at zero weight and was recycled on 2026-10-02."""
import pytest

from sn89_signals import config, hf, scoring

DAY = 86400.0


def _subs(now, n=120, days=20, short_every=0, pairs=("BTCUSD",)):
    """n submissions spread over the last `days` days. short_every=k makes every
    k-th call SHORT; 0 = all LONG (fails diversity)."""
    out = []
    for i in range(n):
        t = now - days * DAY + i * (days * DAY / n)
        d = "SHORT" if short_every and i % short_every == 0 else "LONG"
        out.append((int(t * 1000), pairs[i % len(pairs)], d, 1800))
    return out


def _wins(now, n=12, start_days_ago=3.0):
    return [(now - start_days_ago * DAY + i * 3600, True, False) for i in range(n)]


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(config, "HF_GATED_PROBATION_FROM", 1)
    monkeypatch.setattr(hf, "integrity_ok", lambda hk, now: True)


@pytest.fixture
def disarmed(monkeypatch):
    monkeypatch.setattr(config, "HF_GATED_PROBATION_FROM", 0)
    monkeypatch.setattr(hf, "integrity_ok", lambda hk, now: True)


NOW = 1_800_000_000.0


def _field(blocked_wins=None):
    """A: two-sided earner. B: one-sided, qualified (or as given)."""
    dec = {"A": _wins(NOW), "B": _wins(NOW) if blocked_wins is None else blocked_wins}
    subs = {"A": _subs(NOW, short_every=2), "B": _subs(NOW)}
    uids = {"A": 10, "B": 11}
    return dec, subs, uids


def _w(dec, subs, uids, now=NOW):
    return hf.hf_compute_weights(dec, {k: now - 30 * DAY for k in dec}, uids, now, subs)


def test_fixture_sanity():
    assert hf.hf_diversity(_subs(NOW), NOW)["ok"] is False
    assert hf.hf_diversity(_subs(NOW, short_every=2), NOW)["ok"] is True


def test_disarmed_blocked_miner_gets_nothing(disarmed):
    w = _w(*_field())
    assert w.get(11, 0.0) == 0.0
    assert w.get(10, 0.0) > 0.0


def test_armed_blocked_qualified_miner_keeps_dust_only(armed):
    w = _w(*_field())
    assert w.get(11) == pytest.approx(config.DUST_WEIGHT)
    assert w.get(10, 0.0) > 0.5


def test_armed_changes_others_only_by_the_dust(armed, monkeypatch):
    on = _w(*_field())
    monkeypatch.setattr(config, "HF_GATED_PROBATION_FROM", 0)
    off = _w(*_field())
    assert set(on) - set(off) == {11}
    assert sum(on.values()) == pytest.approx(1.0)
    for u in off:
        assert on.get(u, 0.0) == pytest.approx(off[u] * (1 - config.DUST_WEIGHT), abs=1e-9) \
            or u == config.BURN_UID


def test_never_qualified_blocked_miner_gets_nothing(armed):
    losses = [(NOW - 3 * DAY + i * 3600, False, False) for i in range(12)]
    w = _w(*_field(blocked_wins=losses))
    assert w.get(11, 0.0) == 0.0


def test_floor_closes_with_the_ungated_window(armed):
    # last qualified win ~3d ago: dust lasts to last_win + decay + probation
    dec, subs, uids = _field()
    last = max(t for t, won, _ in dec["B"])
    inside = last + hf.HF_EMISSION_DECAY_S + config.PROBATION_S - DAY
    after = last + hf.HF_EMISSION_DECAY_S + config.PROBATION_S + DAY
    for now, expect in ((inside, True), (after, False)):
        # Keep the original history (eligibility stays where it was) and add fresh
        # calls inside the trailing diversity window: B one-sided, A two-sided.
        s = {"A": subs["A"] + _subs(now, short_every=2),
             "B": subs["B"] + _subs(now)}
        w = hf.hf_compute_weights(dec, {k: NOW - 30 * DAY for k in dec}, uids, now, s)
        assert (w.get(11, 0.0) > 0.0) is expect


def test_integrity_flagged_blocked_miner_gets_nothing(armed, monkeypatch):
    monkeypatch.setattr(hf, "integrity_ok", lambda hk, now: hk != "B")
    w = _w(*_field())
    assert w.get(11, 0.0) == 0.0


def test_stamp_is_as_of_the_cycle_clock(monkeypatch):
    monkeypatch.setattr(hf, "integrity_ok", lambda hk, now: True)
    monkeypatch.setattr(config, "HF_GATED_PROBATION_FROM", int(NOW) + 60)
    assert _w(*_field()).get(11, 0.0) == 0.0
    monkeypatch.setattr(config, "HF_GATED_PROBATION_FROM", int(NOW) - 60)
    assert _w(*_field()).get(11) == pytest.approx(config.DUST_WEIGHT)


def test_lf_compute_weights_unchanged_without_probation_only():
    st = [scoring.MinerState(hotkey="A", uid=10, first_seen_unix=NOW - 30 * DAY,
                             rep_wins=10, rep_decisive=12, trailing_wins=10,
                             qwins=[(NOW - DAY, 1.0)])]
    assert scoring.compute_weights(st, NOW) == scoring.compute_weights(st, NOW, probation_only=[])
