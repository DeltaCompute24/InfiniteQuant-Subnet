#!/usr/bin/env python3
"""SN89 HF anchor cycle — seal → publish → (optionally) commit on chain.

Runs as a daemon. Each pass, for every window the ingest and tick recorder have
sealed but this service has not yet processed:

  1. compute both roots (receipts + ticks) and the window root
  2. publish the receipt log and tick log to the public bucket (if configured)
  3. submit the on-chain anchor commitment (ONLY if an anchor wallet is configured)

The on-chain step is gated on SN89_HF_ANCHOR_WALLET because it needs a hotkey
REGISTERED on the subnet — an unregistered key gets AccountNotAllowedCommit, and
CommitmentOf is one latest-wins slot per hotkey, so the anchor hotkey must anchor
and nothing else. Until that hotkey exists, this runs in PREVIEW posture: windows
are sealed, signed and retained locally, which is sufficient while emission is 0%
and the handful of preview traders trust the operator directly. The fraud-proof
guarantee turns on with the chain anchor, at the same time emissions do.

State: <state_dir>/anchored.txt — window ids already fully processed, so a restart
never re-anchors (which would burn the per-epoch commitment space budget).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
import pathlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sn89_signals import hf

LOG_DIR = Path(os.getenv("SN89_HF_LOG_DIR", "/var/lib/sn89-hf"))
TICK_DIR = Path(os.getenv("SN89_HF_TICK_DIR", str(LOG_DIR / "ticks")))
STATE = Path(os.getenv("SN89_HF_ANCHOR_STATE", str(LOG_DIR / "anchored.txt")))
POLL_S = int(os.getenv("SN89_HF_ANCHOR_POLL_S", "20"))
NETUID = int(os.getenv("SN89_NETUID", "89"))
NETWORK = os.getenv("SN89_NETWORK", "finney")
ANCHOR_WALLET = os.getenv("SN89_HF_ANCHOR_WALLET", "")     # name; empty = preview
ANCHOR_HOTKEY = os.getenv("SN89_HF_ANCHOR_HOTKEY", "default")
PUBLIC_DIR = os.getenv("SN89_HF_PUBLIC_DIR", "")           # webhook-served; empty = no publish


def _log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [hf-anchor] {m}", flush=True)


def _done() -> set:
    try:
        return set(STATE.read_text().split())
    except FileNotFoundError:
        return set()


def _mark(w: int) -> None:
    with STATE.open("a") as f:
        f.write(f"{w}\n")


class Anchorer:
    def __init__(self):
        self.sub = None
        self.wallet = None
        if ANCHOR_WALLET:
            import bittensor as bt
            self.sub = bt.Subtensor(NETWORK)
            self.wallet = bt.Wallet(name=ANCHOR_WALLET, hotkey=ANCHOR_HOTKEY)
            self.wallet.unlock_hotkey()
            _log(f"anchor hotkey {self.wallet.hotkey.ss58_address} on {NETWORK}/{NETUID}")
        else:
            _log("PREVIEW posture — no anchor wallet, sealing + retaining only "
                 "(on-chain anchor turns on with emissions)")

    def _roots(self, w: int):
        rlog = LOG_DIR / f"{w}.jsonl"
        tlog = TICK_DIR / f"{w}.ticks.jsonl"
        if not rlog.exists():
            return None
        receipts = [json.loads(l)["receipt"] for l in rlog.open() if l.strip()]
        ticks = [json.loads(l) for l in tlog.open()] if tlog.exists() else []
        root = hf.merkle_root([hf.leaf(hf.receipt_signing_bytes(
            r["hk"], r["seq"], r["ph"], r["t_recv_us"], r["grid_t0_ms"], r["ing"]))
            for r in sorted(receipts, key=hf.leaf_order_key)])
        troot = hf.tick_root(ticks)
        return {"n": len(receipts), "tick_n": len(ticks),
                "root": root, "tick_root": troot,
                "window_root": hf.window_root(root, troot)}

    def _publish(self, w: int) -> None:
        """Copy the sealed window into the webhook-served public dir so ANY
        validator can fetch the exact grading inputs and reproduce our weights.
        This is what makes mecid-1 replayable rather than trusted — the same role
        R2_PUBLIC_BASE plays for LF blobs."""
        if not PUBLIC_DIR:
            return
        import shutil
        dst = pathlib.Path(PUBLIC_DIR) / str(w)
        dst.mkdir(parents=True, exist_ok=True)
        pairs = ((LOG_DIR / f"{w}.jsonl", dst / "receipts.jsonl"),
                 (TICK_DIR / f"{w}.ticks.jsonl", dst / "ticks.jsonl"),
                 (TICK_DIR / f"{w}.ticks.json", dst / "ticks.json"),
                 (LOG_DIR / f"{w}.anchor.json", dst / "anchor.json"))
        for src, out in pairs:
            if src.exists():
                shutil.copyfile(src, out)
                try:
                    out.chmod(0o644)
                except OSError:
                    pass
        try:
            dst.chmod(0o755)
        except OSError:
            pass

    def _commit(self, w: int, r: dict) -> bool:
        if not self.wallet:
            return False
        data = hf.encode_anchor(w, r["n"], r["tick_n"], r["root"], r["tick_root"])
        ok = self.sub.set_commitment(wallet=self.wallet, netuid=NETUID,
                                     data=data, wait_for_finalization=False)
        _log(f"window {w} on-chain anchor {len(data.encode())}B -> "
             + ("OK" if ok else "FAILED"))
        return bool(ok)

    def sweep(self) -> int:
        done = _done()
        # windows the ingest has sealed (a *.jsonl that is not the state file)
        sealed = sorted(int(p.stem) for p in LOG_DIR.glob("*.jsonl")
                        if p.stem.isdigit() and p.stem not in done)
        n = 0
        for w in sealed:
            r = self._roots(w)
            if r is None:
                continue
            self._publish(w)
            self._commit(w, r)
            _mark(w)
            n += 1
            _log(f"window {w}: n={r['n']} ticks={r['tick_n']} "
                 f"wroot={r['window_root'][:16]}… "
                 f"{'ANCHORED' if self.wallet else 'sealed(preview)'}")
        return n

    def run(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _log(f"watching {LOG_DIR} every {POLL_S}s")
        while True:
            try:
                self.sweep()
            except Exception as e:      # noqa: BLE001
                _log(f"sweep error: {e}")
            time.sleep(POLL_S)


if __name__ == "__main__":
    Anchorer().run()
