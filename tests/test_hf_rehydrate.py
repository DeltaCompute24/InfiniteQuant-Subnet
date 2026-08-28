"""A restart must not un-anchor a call the ingest already signed for.

Accepted receipts sit in `Ingest.windows` until their window seals, so a bounce
mid-window used to drop every call taken since the window opened: signed, ack'd
to the miner, absent from the sealed log, ungradeable forever. `record_live` was
already writing each accept to the live tail, so the recovery data existed; these
tests pin the reader that uses it, and pin the two ways the reader could do more
harm than the bug -- re-sealing a published window, and double-leafing one call.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neurons import hf_ingest                                    # noqa: E402


def _row(hk, seq, t_recv_us, pair="SOLUSD"):
    return {"submit": {"kind": "hf.submit", "v": 1, "hk": hk, "seq": seq,
                       "payload": {"trade_pair": pair, "direction": "SHORT"}},
            "receipt": {"v": 1, "kind": "hf.receipt", "hk": hk, "seq": seq,
                        "ph": "00" * 32, "t_recv_us": t_recv_us,
                        "grid_t0_ms": t_recv_us // 1000, "ing": "ingest-test",
                        "sig_owner": "ab" * 64}}


@pytest.fixture
def ing(tmp_path, monkeypatch):
    """An Ingest with only the two attributes rehydrate touches.

    __init__ needs a keypair and a live metagraph, and neither has anything to do
    with reading a JSONL file off disk.
    """
    monkeypatch.setattr(hf_ingest, "LOG_DIR", tmp_path / "sealed")
    (tmp_path / "sealed").mkdir()
    (tmp_path / "live").mkdir()
    o = hf_ingest.Ingest.__new__(hf_ingest.Ingest)
    o._live_dir = tmp_path / "live"
    o.windows = {}
    o._tmp = tmp_path
    return o


def _tail(ing, w, rows):
    with open(ing._tmp / "live" / f"{w}.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class TestRehydrate:
    W = 1787810400000

    def test_unsealed_window_is_recovered(self, ing):
        """THE regression: the call that was lost on 2026-08-27."""
        _tail(ing, self.W, [_row("5G92", 1787810438257785, 1787810438334776)])
        assert ing.rehydrate_windows() == 1
        assert [e["receipt"]["seq"] for e in ing.windows[self.W]] == [1787810438257785]

    def test_sealed_window_is_never_touched(self, ing):
        """Re-sealing would rewrite a root that may already be on chain."""
        (hf_ingest.LOG_DIR / f"{self.W}.jsonl").write_text("")
        _tail(ing, self.W, [_row("5G92", 1, 100)])
        assert ing.rehydrate_windows() == 0
        assert ing.windows == {}

    def test_one_call_produces_one_leaf(self, ing):
        """A duplicated tail append must not double-count into the Merkle tree."""
        r = _row("5G92", 7, 100)
        _tail(ing, self.W, [r, r, _row("5D8K", 8, 101)])
        assert ing.rehydrate_windows() == 2

    def test_partial_tail_keeps_what_it_can(self, ing):
        """A truncated last line costs that call, not the whole window."""
        p = ing._tmp / "live" / f"{self.W}.jsonl"
        p.write_text(json.dumps(_row("5G92", 1, 100)) + "\n{\"submit\": ")
        assert ing.rehydrate_windows() == 1

    def test_row_that_cannot_be_sealed_is_dropped(self, ing):
        """A receipt short of a field the ROOT is computed over must not reach the
        seal, where it raises inside anchor_loop and stops sealing entirely."""
        good = _row("5G92", 1, 100)
        short = _row("5D8K", 2, 101)
        del short["receipt"]["ing"]
        _tail(ing, self.W, [good, short])
        assert ing.rehydrate_windows() == 1
        assert ing.seal_window(self.W)["n"] == 1

    def test_no_live_dir_is_not_fatal(self, ing):
        ing._live_dir = None
        assert ing.rehydrate_windows() == 0

    def test_recovered_rows_survive_a_seal(self, ing, monkeypatch):
        """End to end: rehydrate then seal writes the call into the anchored log."""
        _tail(ing, self.W, [_row("5G92", 1787810438257785, 1787810438334776),
                            _row("5D8K", 1787810575968192, 1787810575968192)])
        ing.rehydrate_windows()
        anchor = ing.seal_window(self.W)
        assert anchor["n"] == 2
        lines = (hf_ingest.LOG_DIR / f"{self.W}.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2
        # leaf_order_key ordering is (t_recv_us, hk, seq) — the only legal order.
        assert [json.loads(l)["receipt"]["seq"] for l in lines] == [
            1787810438257785, 1787810575968192]
