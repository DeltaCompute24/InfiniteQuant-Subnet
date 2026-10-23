"""Keep-alive floor (config.KEEPALIVE_*, Whit 2026-10-23)."""
from sn89_signals import config, replay, scoring

NOW = 1_900_000_000.0
BLOCK = 10_000_000


def _st(hk, uid, first_seen, qwins=()):
    return scoring.MinerState(hotkey=hk, uid=uid, first_seen_unix=first_seen,
                              rep_wins=0, rep_decisive=0, trailing_wins=0,
                              qwins=list(qwins))


def test_off_before_the_arm(monkeypatch):
    monkeypatch.setattr(config, "KEEPALIVE_FROM_UNIX", int(NOW) + 1)
    assert replay.keepalive_uids({"a": 5}, NOW, BLOCK, {5: BLOCK - 1}, {"a": [NOW]}) == set()
    monkeypatch.setattr(config, "KEEPALIVE_FROM_UNIX", 0)
    assert replay.keepalive_uids({"a": 5}, NOW, BLOCK, {5: BLOCK - 1}, {"a": [NOW]}) == set()


def test_fresh_registration_or_recent_call(monkeypatch):
    monkeypatch.setattr(config, "KEEPALIVE_FROM_UNIX", int(NOW) - 10)
    uids = {"fresh": 1, "old_active": 2, "old_idle": 3, "stale_call": 4, "burn": config.BURN_UID}
    reg = {1: BLOCK - 100, 2: BLOCK - 10**6, 3: BLOCK - 10**6, 4: BLOCK - 10**6,
           config.BURN_UID: BLOCK - 1}
    calls = {"old_active": [NOW - 3600],
             "stale_call": [NOW - config.KEEPALIVE_ACTIVE_S - 1],
             "burn": [NOW]}
    assert replay.keepalive_uids(uids, NOW, BLOCK, reg, calls) == {1, 2}
    # the registration window is IMMUNITY_S of 12 s blocks
    reg[1] = BLOCK - config.KEEPALIVE_REG_BLOCKS
    assert 1 not in replay.keepalive_uids(uids, NOW, BLOCK, reg, calls)


def test_floor_only_for_miners_not_otherwise_earning():
    old = NOW - 100 * 86400
    earning = _st("e", 1, old, qwins=[(NOW - 3600, 1.0)])
    idle = _st("i", 2, old)                       # past warmup, never qualified
    w0 = scoring.compute_weights([earning, idle], NOW)
    assert 2 not in w0
    w = scoring.compute_weights([earning, idle], NOW, keepalive_uids={1, 2, 3})
    assert w[2] == w[3] and w[2] > 0              # 3: registered, no history at all
    assert w[1] > w[2]                            # the earner keeps its share, no dust swap
    w = scoring.compute_weights([earning, idle], NOW, keepalive_uids={2},
                                excluded_uids={2})
    assert 2 not in w


def test_replay_drops_eliminated_and_struck(monkeypatch):
    old = NOW - 100 * 86400
    meta = {"a": {"first_seen_unix": old, "strikes": 0},
            "s": {"first_seen_unix": old, "strikes": config.STRIKE_LIMIT}}
    uid_by_hk = {"a": 7, "s": 8}
    w = replay.weights_from_journal([], meta, uid_by_hk, NOW, keepalive={7, 8})
    assert 7 in w and 8 not in w
    assert replay.weights_from_journal([], meta, uid_by_hk, NOW) == \
        replay.weights_from_journal([], meta, uid_by_hk, NOW, keepalive=None)
