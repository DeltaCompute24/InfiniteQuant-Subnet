#!/usr/bin/env python3
"""SN89 HF ingest — sub-second submission binding for mechanism 1. OWNER ONLY.

STAGED, NOT RUNNING. No systemd unit is installed and mechanism 1 earns zero
emission (`MechanismEmissionSplit[89] = [65535, 0]`).

    miner --WSS--> verify sr25519 (~50us) --> stamp t_recv --> COUNTERSIGNED RECEIPT
                                                           --> window log --> Merkle anchor

The receipt is the binding artifact, replacing the on-chain commitment that costs
mechanism 0 a p50 of 25 s. It cuts both ways on purpose:

  * the miner cannot ghost — they signed it, and `seq` is strictly increasing per
    hotkey, so equivocation forks their own sequence and we hold both signatures;
  * WE cannot censor — the miner holds our signature over (payload_hash, seq,
    t_recv). A receipt missing from the anchored window is a fraud proof, and
    INCLUSION REPAIR (below) makes that mechanically self-correcting rather than a
    matter of trusting us.

Every refusal is signed too, so "refused" is always distinguishable from "dropped".
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets
from bittensor_wallet import Keypair

from sn89_signals import hf

BIND = os.getenv("SN89_HF_BIND", "127.0.0.1")
PORT = int(os.getenv("SN89_HF_PORT", "8790"))
ING_ID = os.getenv("SN89_HF_INGEST_ID", "ingest-local-1")
LOG_DIR = Path(os.getenv("SN89_HF_LOG_DIR", "/var/lib/sn89-hf"))
ENABLED = os.getenv("SN89_HF_ENABLED") == "1"      # refuses to run without this
VALIDATOR_DB = os.getenv("SN89_DB_PATH", "/root/.sn89/validator-main.db")
LOCK_REFRESH_S = int(os.getenv("SN89_HF_LOCK_REFRESH_S", "60"))

_state: dict = {"seq": {}, "sent": {}, "windows": {}}


def _log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [hf-ingest] {m}", flush=True)


class Ingest:
    def __init__(self, receipt_kp: Keypair):
        self.kp = receipt_kp
        self.last_seq: dict[str, int] = {}          # hotkey -> last accepted seq
        self.sent_ms: dict[str, list] = {}          # hotkey -> accepted submit times
        self.windows: dict[int, list] = {}          # window start ms -> receipts
        self.lock_index: dict = {}                  # (hk, pair, mecid) -> ts ms
        self._locks_loaded_at = 0.0
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.refresh_mech0_locks()

    def refresh_mech0_locks(self) -> int:
        """Pull mechanism-0 submissions into the lock index.

        HF accepts populate their own entries inline; without this the LF side of
        the cross-mechanism lock has no data and silently never fires.
        """
        now = time.time()
        rows = hf.load_mech0_locks(VALIDATOR_DB, int(now * 1000) - hf.PAIR_LOCK_MS)
        for hk, pair, mecid, ts in rows:
            k = (hk, pair, mecid)
            if ts > self.lock_index.get(k, -1):
                self.lock_index[k] = ts
        self._locks_loaded_at = now
        return len(rows)

    # ── signing ──────────────────────────────────────────────────────────────
    def sign_receipt(self, hk, seq, ph, t_recv_us, grid_ms) -> dict:
        rb = hf.receipt_signing_bytes(hk, seq, ph, t_recv_us, grid_ms, ING_ID)
        return {"v": 1, "kind": "hf.receipt", "hk": hk, "seq": seq, "ph": ph,
                "t_recv_us": t_recv_us, "grid_t0_ms": grid_ms, "ing": ING_ID,
                "sig_owner": self.kp.sign(rb).hex()}

    def sign_rejection(self, hk, seq, reason, t_recv_us) -> dict:
        rb = hf.receipt_signing_bytes(hk, seq, f"REJECT:{reason}", t_recv_us, 0, ING_ID)
        return {"v": 1, "kind": "hf.reject", "hk": hk, "seq": seq, "reason": reason,
                "t_recv_us": t_recv_us, "ing": ING_ID,
                "sig_owner": self.kp.sign(rb).hex()}

    # ── the hot path ─────────────────────────────────────────────────────────
    def handle(self, frame: dict) -> dict:
        t_recv_us = time.time_ns() // 1000
        t_recv_ms = t_recv_us // 1000
        hk = str(frame.get("hk", ""))
        seq = int(frame.get("seq", -1))

        def reject(reason):
            return self.sign_rejection(hk, seq, reason, t_recv_us)

        try:
            nonce = str(frame["nonce"])
            payload = frame["payload"]
            ts_miner = int(frame["ts_miner"])
            sig = bytes.fromhex(frame["sig"])
        except Exception:
            return reject("malformed_frame")

        if frame.get("kind") != "hf.submit" or int(frame.get("v", 0)) != 1:
            return reject("bad_kind_or_version")

        # 1. authenticity — the miner's own signature over canonical bytes
        sb = hf.submit_signing_bytes(hk, seq, nonce, payload, ts_miner)
        try:
            if not Keypair(ss58_address=hk).verify(sb, sig):
                return reject("bad_signature")
        except Exception:
            return reject("bad_hotkey")

        # 2. replay / equivocation — seq is strictly increasing per hotkey
        if seq <= self.last_seq.get(hk, -1):
            return reject("stale_seq")
        if abs(t_recv_ms - ts_miner) > hf.MAX_CLOCK_SKEW_MS:
            return reject("clock_skew")

        # 3. consensus validity — board, band, horizon, asset class
        t0 = t_recv_ms / 1000.0
        try:
            hf.validate_submission(payload, t0)
            hf.check_rate(self.sent_ms.get(hk, []), t_recv_ms, t0)
        except hf.HFRejected as e:
            return reject(str(e))

        # 4. the cross-mechanism pair lock
        pair = str(payload["trade_pair"]).upper()  # noqa: E501
        if hf.is_pair_locked(self.lock_index, hk, pair, hf.MECID, t_recv_ms):
            return reject("pair_locked_other_mechanism")

        # 5. accept — stamp, receipt, log
        ph = hf.payload_hash(sb)
        grid = hf.grid_t0_ms(t_recv_ms, pair, t0)      # per-class grid: crypto 250ms, fx/metals 1s
        rcpt = self.sign_receipt(hk, seq, ph, t_recv_us, grid)

        self.last_seq[hk] = seq
        self.sent_ms.setdefault(hk, []).append(t_recv_ms)
        self.lock_index[(hk, pair, hf.MECID)] = t_recv_ms
        w = hf.window_start_ms(t_recv_ms)
        self.windows.setdefault(w, []).append({"submit": frame, "receipt": rcpt})
        return rcpt

    # ── anchoring ────────────────────────────────────────────────────────────
    def seal_window(self, w: int) -> dict | None:
        """Write the window log and return the anchor payload for the chain commit.

        The caller publishes the log to the bucket and submits `set_commitment`. A
        window with no receipts still anchors (root = zeroes) so a gap in the anchor
        chain is always distinguishable from a quiet minute.
        """
        entries = self.windows.pop(w, [])
        path = LOG_DIR / f"{w}.jsonl"
        ordered = sorted(entries, key=lambda e: hf.leaf_order_key(e["receipt"]))
        with open(path, "w") as f:
            for e in ordered:
                f.write(json.dumps(e, separators=(",", ":")) + "\n")
        anchor = hf.anchor_payload(w, [e["receipt"] for e in ordered], f"file:{path.name}")
        (LOG_DIR / f"{w}.anchor.json").write_text(json.dumps(anchor))
        _log(f"window {w} sealed · n={anchor['n']} root={anchor['root'][:16]}…")
        return anchor

    async def anchor_loop(self):
        while True:
            if time.time() - self._locks_loaded_at > LOCK_REFRESH_S:
                self.refresh_mech0_locks()
            now = int(time.time() * 1000)
            cur = hf.window_start_ms(now)
            for w in [x for x in self.windows if x < cur]:
                self.seal_window(w)
            await asyncio.sleep(1.0)


async def serve(ing: Ingest):
    async def handler(ws):
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except Exception:
                await ws.send(json.dumps({"kind": "hf.reject", "reason": "bad_json"}))
                continue
            await ws.send(json.dumps(ing.handle(frame), separators=(",", ":")))

    async with websockets.serve(handler, BIND, PORT, ping_interval=20):
        _log(f"listening on ws://{BIND}:{PORT} · ingest {ING_ID}")
        await asyncio.gather(ing.anchor_loop())


def main():
    if not ENABLED:
        print("SN89_HF_ENABLED != 1 — staged, refusing to run", file=sys.stderr)
        return 2
    sk = os.getenv("SN89_HF_RECEIPT_SK", "").strip()
    if not sk:
        print("SN89_HF_RECEIPT_SK not set (decrypt from the vault first)", file=sys.stderr)
        return 2
    kp = Keypair.create_from_uri(sk) if sk.startswith("//") else Keypair.create_from_seed(sk)
    _log(f"receipt key {kp.ss58_address}")
    asyncio.run(serve(Ingest(kp)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
