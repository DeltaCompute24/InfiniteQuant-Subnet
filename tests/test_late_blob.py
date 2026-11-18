"""Selective reveal: a blob uploaded after the outcome can be watched, or swapped
for one that cannot open, must not turn a loss into a void."""
import json
import sqlite3
import sys
import time
import types

from sn89_signals import bucket, chain, config, crypto

HK = "5" + "A" * 47


def _validator():
    if "bittensor" not in sys.modules:
        sys.modules["bittensor"] = types.ModuleType("bittensor")
    from neurons.validator import SCHEMA, Validator
    v = Validator.__new__(Validator)
    v.db = sqlite3.connect(":memory:")
    v.db.executescript(SCHEMA)
    return v


def _seal(v, commit, blob, matured_ago_s=10):
    rnd = crypto.target_round(time.time() - matured_ago_s)
    v.db.execute(
        "INSERT INTO signals (commit_hex,hotkey,round,url_tag,first_seen_block,"
        "t0_unix,blob_json,status) VALUES (?,?,?,?,?,?,?,'sealed')",
        (commit, HK, rnd, "tag", 1, time.time() - matured_ago_s - 86_400,
         json.dumps(blob) if blob is not None else None))
    v.db.commit()


def _row(v, commit):
    return v.db.execute("SELECT status, exit_reason, void_reason FROM signals "
                        "WHERE commit_hex=?", (commit,)).fetchone()


def test_blob_naming_another_commitment_is_a_loss_not_a_void():
    v = _validator()
    blob = {"owner_pk": config.OWNER_PK_HEX, "hk": HK, "commit": "bb" * 32,
            "W_owner": {}, "W_time": {}}
    _seal(v, "aa" * 32, blob)
    v.reveal()
    assert _row(v, "aa" * 32) == ("lost", "bad_reveal", None)


def test_blob_naming_the_right_commitment_is_not_forfeited_by_that_rule(monkeypatch):
    v = _validator()
    blob = {"owner_pk": config.OWNER_PK_HEX, "hk": HK, "commit": "AA" * 32,
            "W_owner": {}, "W_time": {}}
    _seal(v, "aa" * 32, blob)
    monkeypatch.setattr(v, "_drand_sig", lambda rnd: None, raising=False)
    v.reveal()                                   # waits for drand; still sealed
    assert _row(v, "aa" * 32) == ("sealed", None, None)


def _serve(monkeypatch, index, blob):
    def fake(url, *a, **k):
        return index if url.endswith("/index.json") else blob
    monkeypatch.setattr(bucket, "fetch", fake)
    monkeypatch.setattr(config, "R2_EXTRA_BASES", ())


def _tag(nonce):
    return chain.url_tag(bucket.blob_url(HK, nonce, base=config.R2_PUBLIC_BASE))


def test_blob_stored_long_after_commit_is_ignored(monkeypatch):
    v = _validator()
    t0 = 1_800_000_000
    _serve(monkeypatch, {"nonces": ["n1"], "received": {"n1": t0 + config.BLOB_LATE_S + 1}},
           {"v": 1})
    assert v._find_blob(HK, _tag("n1"), None, t0) is None


def test_blob_stored_before_or_just_after_commit_is_accepted(monkeypatch):
    v = _validator()
    t0 = 1_800_000_000
    for got in (t0 - 8, t0 + config.BLOB_LATE_S):
        _serve(monkeypatch, {"nonces": ["n1"], "received": {"n1": got}}, {"v": 1})
        assert v._find_blob(HK, _tag("n1"), None, t0) == {"v": 1}


def test_host_without_receive_times_and_batches_are_unaffected(monkeypatch):
    v = _validator()
    t0 = 1_800_000_000
    _serve(monkeypatch, {"nonces": ["n1"]}, {"v": 1})
    assert v._find_blob(HK, _tag("n1"), None, t0) == {"v": 1}
    _serve(monkeypatch, {"nonces": ["n1"], "received": {"n1": t0 + 99_999}}, {"v": 1})
    assert v._find_blob(HK, _tag("n1"), 1 << 20) == {"v": 1}      # batch: no t0
