"""HF anchor v2 (agent books) in neurons/hf_anchor.py: flag off is v1 exactly; flag on
binds agent receipts + quotes, anchors quiet windows, records the inclusion time; and none
of it depends on HF scoring being live."""
import hashlib
import importlib.util
import json
import pathlib
import time
from types import SimpleNamespace

import pytest

from sn89_signals import agent_books as ab
from sn89_signals import agent_books_io as abio
from sn89_signals import config, hf

_p = pathlib.Path(__file__).resolve().parent.parent / "neurons" / "hf_anchor.py"
_spec = importlib.util.spec_from_file_location("hf_anchor_v2_under_test", _p)
A = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(A)

ENT = "5Hdvy1Msc9Su6U7kcJQRFnc39NNVumAcPnqFLoHqZ4Uix6g3"
N0 = config.AGENT_ACCOUNT_MIN
HK = "5FCN4P1KhQKzfHSj6QXwMeGjGDWkbuL8AQDtwx81F5bqxvoR"


def fake_verify(hk, msg, sig_hex):
    return sig_hex == hashlib.sha256(hk.encode() + msg).hexdigest()


def rcpt(n, seq, t, targets):
    r = {"v": 1, "entity": ENT, "account": f"{ENT}_{n}", "seq": seq, "t_recv_ms": t,
         "targets": targets}
    r["sig"] = hashlib.sha256(ENT.encode() + ab.receipt_signing_bytes(r)).hexdigest()
    return r


class FakeChain:
    """set_commitment + CommitmentOf + Timestamp, enough for _commit / _commit_v2."""

    def __init__(self, ok=True):
        self.ok, self.commits, self.block = ok, [], 1000
        self.substrate = self

    def set_commitment(self, wallet, netuid, data, wait_for_finalization=False):
        self.commits.append(data)
        if self.ok:
            self.block += 1
            self.cur = (data, self.block)
        return SimpleNamespace(success=self.ok, message="" if self.ok else "SpaceLimitExceeded")

    def query(self, module, fn, params=None, block_hash=None):
        if fn == "CommitmentOf":
            d, b = self.cur
            return SimpleNamespace(value={"block": b, "info": {"fields": [[{
                f"Raw{len(d)}": "0x" + d.encode().hex()}]]}})
        if fn == "Now":
            return SimpleNamespace(value=1_700_000_000_000 + int(block_hash) * 12_000)
        raise KeyError(fn)

    def get_block_hash(self, b):
        return b


def _win(ago):
    now = int(time.time() * 1000)
    return (now // A.WINDOW_MS - ago) * A.WINDOW_MS


def _receipt_line(t0):
    return json.dumps({"receipt": {"hk": "5Hp3N8", "seq": 1, "ph": "ab" * 16,
                                   "t_recv_us": t0 * 1000, "grid_t0_ms": t0, "ing": "5FTc1V"}})


def _ticks(w, n=4):
    return [{"a": "BTCUSD", "b": 64000.0 + i - 1, "k": 64000.0 + i + 1, "p": 64000.0 + i,
             "t": w + i} for i in range(n)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    log, tick, pub, books = (tmp_path / x for x in ("log", "ticks", "pub", "books"))
    for d in (log, tick, pub, books):
        d.mkdir()
    monkeypatch.setattr(A, "LOG_DIR", log)
    monkeypatch.setattr(A, "TICK_DIR", tick)
    monkeypatch.setattr(A, "STATE", tmp_path / "anchored.txt")
    monkeypatch.setattr(A, "PUBLIC_DIR", str(pub))
    monkeypatch.setattr(A, "V2_READBACK_SLEEP_S", 0)
    monkeypatch.setattr(config, "AGENT_BOOKS_RECEIPTS_DIR", str(books))
    monkeypatch.setattr(abio, "sr25519_verify", fake_verify)
    monkeypatch.setattr(abio.AgentReceiptIndex.__init__, "__defaults__",
                        (fake_verify, 2 * 86_400_000))
    return log, tick, pub, books


def _anchorer(chain=None):
    a = A.Anchorer()
    a.sub = chain
    a.wallet = (SimpleNamespace(hotkey=SimpleNamespace(ss58_address=HK))
                if chain is not None else None)
    return a


def _seed(log, tick, books):
    """Three windows past the margin: one with an HF/markets receipt, one quiet, one quiet
    with an agent receipt."""
    w1, w2, w3 = (_win(A.TICK_SETTLE_WINDOWS + k) for k in (4, 3, 2))
    (log / f"{w1}.jsonl").write_text(_receipt_line(w1) + "\n")
    for w in (w1, w2, w3):
        (tick / f"{w}.ticks.jsonl").write_text(
            "\n".join(json.dumps(t) for t in _ticks(w)) + "\n")
    r = rcpt(N0 + 1, 1, w3 + 1500, {"BTCUSD": 0.1})
    (books / f"{N0 + 1}.jsonl").write_text(json.dumps(r) + "\n")
    return w1, w2, w3, r


def test_flag_off_is_v1_exactly(env, monkeypatch):
    log, tick, pub, books = env
    monkeypatch.setattr(config, "AGENT_ANCHOR_V2_FROM_UNIX", 0)
    w1, w2, w3, _ = _seed(log, tick, books)
    ch = FakeChain()
    assert _anchorer(ch).sweep() == 3
    # only the receipt-bearing window is committed, in the v1 encoding, byte for byte
    r = A.Anchorer._roots(None, w1)
    assert ch.commits == [hf.encode_anchor(w1, r["n"], r["tick_n"], r["root"], r["tick_root"])]
    for w in (w1, w2, w3):
        files = {p.name for p in (pub / str(w)).iterdir()}
        assert files == {"receipts.jsonl", "ticks.jsonl"}
        assert not (log / f"{w}.anchor-v2.json").exists()
        assert not (log / f"{w}.agent.jsonl").exists()


def test_flag_armed_after_these_windows_is_still_v1(env, monkeypatch):
    log, tick, pub, books = env
    w1, w2, w3, _ = _seed(log, tick, books)
    monkeypatch.setattr(config, "AGENT_ANCHOR_V2_FROM_UNIX", (w3 + A.WINDOW_MS) // 1000)
    ch = FakeChain()
    assert _anchorer(ch).sweep() == 3
    assert len(ch.commits) == 1 and ch.commits[0].startswith("sn89hf:1:")


def test_v2_anchors_quiet_windows_binds_agent_receipts_and_records_time(env, monkeypatch):
    log, tick, pub, books = env
    w1, w2, w3, r = _seed(log, tick, books)
    monkeypatch.setattr(config, "AGENT_ANCHOR_V2_FROM_UNIX", w1 // 1000)
    ch = FakeChain()
    assert _anchorer(ch).sweep() == 3
    assert len(ch.commits) == 3 and all(c.startswith("sn89hf:2:") for c in ch.commits)
    doc = json.loads((log / f"{w3}.anchor-v2.json").read_text())
    assert doc["agent_n"] == 1 and doc["agent_root"] == ab.receipts_root([r])
    assert doc["quote_root"] == hf.quote_root(_ticks(w3))
    assert doc["anchor"] == ch.commits[2] and doc["anchor_block"] == 1003
    assert doc["anchor_ts_ms"] == 1_700_000_000_000 + 1003 * 12_000
    assert hf.verify_anchor(doc["anchor"], doc["root"], doc["tick_root"], doc["n"],
                            doc["tick_n"], quote_root_hex=doc["quote_root"],
                            agent_root_hex=doc["agent_root"], agent_n=doc["agent_n"])
    assert not hf.verify_anchor(doc["anchor"], doc["root"], doc["tick_root"], doc["n"],
                                doc["tick_n"])                     # v2 needs its halves
    pubd = pub / str(w3)
    assert {"agent.jsonl", "anchor-v2.json", "ticks.jsonl"} <= {p.name for p in pubd.iterdir()}
    assert json.loads((pubd / "agent.jsonl").read_text().strip()) == r
    # the weekly loader accepts exactly what was anchored ...
    win = {w3: {"agent_root": doc["agent_root"], "agent_n": 1,
                "anchor_ts_ms": w3 + A.WINDOW_MS + 60_000}}
    kept, drops = abio.filter_anchored([r], win)
    assert kept == [r] and not drops


def test_v2_refused_commit_is_retried_not_retired(env, monkeypatch):
    log, tick, pub, books = env
    w1, w2, w3, _ = _seed(log, tick, books)
    monkeypatch.setattr(config, "AGENT_ANCHOR_V2_FROM_UNIX", w1 // 1000)
    a = _anchorer(FakeChain(ok=False))
    assert a.sweep() == 0
    assert not A._done()
    doc = json.loads((log / f"{w3}.anchor-v2.json").read_text())
    assert doc["anchor_ts_ms"] is None


def test_anchoring_does_not_depend_on_hf_being_live(env, monkeypatch):
    """HF scoring ends (config.HF_END_FROM_UNIX) but iq-sn89-hf-anchor keeps running."""
    log, tick, pub, books = env
    w1, w2, w3, _ = _seed(log, tick, books)
    monkeypatch.setattr(config, "HF_END_FROM_UNIX", 1)              # HF long over
    monkeypatch.setattr(config, "AGENT_ANCHOR_V2_FROM_UNIX", w1 // 1000)
    assert config.hf_ended_as_of(time.time())
    ch = FakeChain()
    assert _anchorer(ch).sweep() == 3 and len(ch.commits) == 3
    # and v1, with HF over and no v2: still seals, publishes, commits receipt windows
    for p in list(log.iterdir()):
        if p.suffix == ".txt" or "anchor-v2" in p.name or "agent" in p.name:
            p.unlink()
    (A.STATE).unlink()
    monkeypatch.setattr(config, "AGENT_ANCHOR_V2_FROM_UNIX", 0)
    ch = FakeChain()
    assert _anchorer(ch).sweep() == 3 and len(ch.commits) == 1


def test_source_has_no_hf_end_gate():
    src = _p.read_text()
    assert "hf_ended" not in src and "HF_END" not in src.replace("HF_END_FROM_UNIX)", "")


def test_receipt_index_is_incremental_and_keeps_partial_lines(env):
    log, tick, pub, books = env
    w = _win(10)
    idx = abio.AgentReceiptIndex(str(books), verify=fake_verify)
    r1, r2 = rcpt(N0 + 1, 1, w + 10, {"BTCUSD": 0.1}), rcpt(N0 + 1, 2, w + 20, {"BTCUSD": 0.2})
    f = books / f"{N0 + 1}.jsonl"
    line2 = json.dumps(r2)
    f.write_text(json.dumps(r1) + "\n" + line2[:30])              # torn tail
    idx.refresh()
    assert idx.window(w) == [r1]
    with f.open("a") as fh:
        fh.write(line2[30:] + "\n" + json.dumps(dict(r1, sig="00")) + "\n")
    idx.refresh()
    assert idx.window(w) == [r1, r2] and idx.drops == {"bad_signature": 1}


def test_hf_decode_accepts_both_versions():
    v1 = hf.encode_anchor(1793727180000, 3, 10, "11" * 32, "22" * 32)
    assert hf.decode_anchor(v1) == {"w": 1793727180000, "n": 3, "tick_n": 10,
                                    "window_root": hf.window_root("11" * 32, "22" * 32)}
    assert hf.verify_anchor(v1, "11" * 32, "22" * 32, 3, 10)
    v2 = hf.encode_anchor_v2(1793727180000, 3, 10, 2, "11" * 32, "22" * 32, "33" * 32,
                             "44" * 32)
    d = hf.decode_anchor(v2)
    assert d["v"] == 2 and d["agent_n"] == 2 and d["n"] == 3
