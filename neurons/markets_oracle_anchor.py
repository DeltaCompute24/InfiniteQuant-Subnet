#!/usr/bin/env python3
"""SN89 Markets oracle anchor: batch sealed oracle windows into a hash chain and commit its head.

Every oracle.BATCH_WINDOWS consecutive windows (aligned to the batch grid) form a batch:
    {"w0", "n", "root", "prev", "link"}      see sn89_signals/oracle.py
appended to DATA_DIR/batches.jsonl, which the Markets service publishes as the chain. The newest
link is then committed on chain as `sn89mo:1:<w0 s>:<n>:<link>` from the Markets entity hotkey
(UID 199). The commitment slot is latest-wins, and that is enough: the link commits to every batch
before it, so one read of the slot verifies the whole published history (scripts/verify_markets_oracle.py).

A window the recorder never sealed (it was down) is sealed EMPTY once it is GAP_SEAL_S old: an
empty window is an honest "no observations", which grading turns into a VOID through the max-age
rule, and it keeps the chain contiguous. A failed commitment is retried with the next batch; the
chain itself never depends on the chain write succeeding.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sn89_signals import oracle  # noqa: E402

DATA_DIR = Path(os.getenv("SN89_MARKETS_ORACLE_DIR", "/var/lib/sn89-markets-oracle"))
BATCHES = DATA_DIR / "batches.jsonl"
NETUID = int(os.getenv("SN89_NETUID", "89"))
NETWORK = os.getenv("SN89_NETWORK", "finney")
WALLET = os.getenv("SN89_MARKETS_ORACLE_ANCHOR_WALLET", "")       # empty = chain only, no commit
HOTKEY = os.getenv("SN89_MARKETS_ORACLE_ANCHOR_HOTKEY", "entity")
POLL_S = 20
GAP_SEAL_S = 600


def _log(m: str) -> None:
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {m}", flush=True)


def load_batches() -> list[dict]:
    if not BATCHES.exists():
        return []
    return [json.loads(l) for l in BATCHES.read_text(encoding="utf-8").splitlines() if l.strip()]


def sealed_root(w: int) -> str | None:
    meta = DATA_DIR / str(w) / "oracle.json"
    if not meta.exists():
        return None
    return json.loads(meta.read_text(encoding="utf-8"))["root"]


def seal_empty(w: int) -> str:
    out = DATA_DIR / str(w)
    out.mkdir(exist_ok=True)
    (out / "oracle.jsonl").write_text("", encoding="utf-8")
    root = oracle.window_root([])
    (out / "oracle.json").write_text(json.dumps({"w": w, "n": 0, "root": root}, separators=(",", ":")),
                                     encoding="utf-8")
    for p in (out, out / "oracle.jsonl", out / "oracle.json"):
        p.chmod(0o755 if p.is_dir() else 0o644)
    _log(f"window {w} was never sealed by the recorder: sealed EMPTY")
    return root


class Anchor:
    def __init__(self):
        self.sub = self.wallet = None
        if WALLET:
            import bittensor as bt
            self.sub = bt.Subtensor(NETWORK)
            self.wallet = bt.Wallet(name=WALLET, hotkey=HOTKEY)
            self.wallet.unlock_hotkey()
            _log(f"anchor hotkey {self.wallet.hotkey.ss58_address} on {NETWORK}/{NETUID}")
        else:
            _log("no anchor wallet: building and publishing the chain only")

    def first_window(self) -> int | None:
        ws = sorted(int(p.name) for p in DATA_DIR.iterdir() if p.is_dir() and p.name.isdigit())
        return ws[0] if ws else None

    def sweep(self) -> None:
        batches = load_batches()
        span = oracle.BATCH_WINDOWS * oracle.WINDOW_MS
        if batches:
            w0 = int(batches[-1]["w0"]) + span
            prev = batches[-1]["link"]
        else:
            first = self.first_window()
            if first is None:
                return
            w0 = -(-first // span) * span                    # first full batch on the grid
            prev = oracle.GENESIS
        now_ms = time.time() * 1000
        new = None
        while True:
            ws = [w0 + i * oracle.WINDOW_MS for i in range(oracle.BATCH_WINDOWS)]
            roots = []
            for w in ws:
                r = sealed_root(w)
                if r is None and now_ms >= w + oracle.WINDOW_MS + GAP_SEAL_S * 1000:
                    r = seal_empty(w)
                roots.append(r)
            if any(r is None for r in roots):
                break
            root = oracle.batch_root(list(zip(ws, roots)))
            link = oracle.chain_link(prev, w0, len(ws), root)
            new = {"w0": w0, "n": len(ws), "root": root, "prev": prev, "link": link}
            with open(BATCHES, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(new, separators=(",", ":")) + "\n")
            BATCHES.chmod(0o644)
            _log(f"batch {w0} ({len(ws)} windows) link {link[:16]}…")
            prev, w0 = link, w0 + span
        if new is not None and self.wallet is not None:
            self.commit(new)

    def commit(self, b: dict) -> None:
        data = oracle.encode_anchor(b["w0"], b["n"], b["link"])
        try:
            ok = self.sub.set_commitment(wallet=self.wallet, netuid=NETUID, data=data,
                                         wait_for_inclusion=True, wait_for_finalization=False)
        except Exception as e:                               # noqa: BLE001
            ok = False
            _log(f"commit raised {type(e).__name__}: {e}")
        _log(f"commit {b['w0']} -> {'OK' if ok else 'FAILED (the next batch carries it)'}")

    def run(self) -> None:
        while True:
            try:
                self.sweep()
            except Exception as e:                           # noqa: BLE001
                _log(f"sweep failed: {type(e).__name__}: {e}")
            time.sleep(POLL_S)


if __name__ == "__main__":
    Anchor().run()
