#!/usr/bin/env python3
"""SN89 HF ingest — sub-second submission binding for mechanism 1. OWNER ONLY.

LIVE on mainnet 89 since 2026-07-23 17:28 UTC (`iq-sn89-hf-ingest.service`), and
mechanism 1 takes ~50% of emission (`MechanismEmissionSplit[89] = [32768, 32767]`).
This docstring used to say "STAGED, NOT RUNNING … mechanism 1 earns zero" — it was
stale for a day while the thing was live and earning.

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
NETUID = int(os.getenv("SN89_NETUID", "89"))
NETWORK = os.getenv("SN89_NETWORK", "finney")
# Ingest verified the miner's SIGNATURE but never that the hotkey exists on the
# subnet, so any generated keypair could take a countersigned receipt, land in a
# Merkle-anchored window and appear on the PUBLIC leaderboard. Four of the six
# "traders" on the HF board on 2026-07-24 were exactly that — unregistered on 89
# and with no coldkey owner on any subnet (our own bring-up probes, seq 7 and
# seq 99 on a single submission each). They could never earn, because mecid-1
# weights are keyed by UID, but they consumed board space and the public record,
# and the same door was open to anyone.
REQUIRE_REGISTERED = os.getenv("SN89_HF_REQUIRE_REGISTERED", "1") == "1"
REG_REFRESH_S = int(os.getenv("SN89_HF_REG_REFRESH_S", "300"))

_state: dict = {"seq": {}, "sent": {}, "windows": {}}


def _log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [hf-ingest] {m}", flush=True)


class Ingest:
    # Class-level defaults so an Ingest built via object.__new__ (the test helpers
    # skip __init__ to avoid touching the network and the validator DB) still has
    # these before _conn()/_drop_conn()/prune() can be reached.
    _subtensor = None
    _pruned_at = 0.0

    def __init__(self, receipt_kp: Keypair):
        self.kp = receipt_kp
        self.last_seq: dict[str, int] = {}          # hotkey -> last accepted seq
        self.sent_ms: dict[str, list] = {}          # hotkey -> accepted submit times
        self.windows: dict[int, list] = {}          # window start ms -> receipts
        self.lock_index: dict = {}                  # (hk, pair, mecid) -> ts ms
        self._locks_loaded_at = 0.0
        self.registered: set = set()                # hotkeys with a UID on NETUID
        self._reg_loaded_at = 0.0
        self._subtensor = None                      # reused across refreshes — see _conn()
        self._pruned_at = 0.0
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.refresh_mech0_locks()
        if REQUIRE_REGISTERED and not self.refresh_registered():
            # Starting ungated would silently reopen the hole this gate closes,
            # and an ungated ingest is indistinguishable from a working one until
            # someone reads the leaderboard. Refuse to run instead.
            raise SystemExit(
                "hf-ingest: could not load the registered-hotkey set at startup "
                "and SN89_HF_REQUIRE_REGISTERED=1 — refusing to run ungated")

    def refresh_registered(self) -> int:
        """Hotkeys holding a UID on the subnet. BLOCKING — never call on the hot
        path; the anchor loop refreshes it in a thread.

        Keeps the last known good set on failure rather than emptying it: an RPC
        blip must not reject every real miner's sub-second submission. An EMPTY
        result is treated as failure for the same reason — "metagraph returned
        nothing" and "nobody is registered" are the same bytes, and this file has
        already been bitten once by that equivalence.
        """
        try:
            hks = set(self._conn().metagraph(NETUID).hotkeys)
        except Exception as e:      # noqa: BLE001
            # Drop the handle so a wedged connection cannot poison every future
            # refresh; _conn() rebuilds it on the next pass.
            self._drop_conn()
            _log(f"!! REGISTERED-SET REFRESH FAILED — keeping last known "
                 f"({len(self.registered)} hotkeys): {e}")
            return 0
        if not hks:
            _log("!! metagraph returned an EMPTY hotkey set — keeping last known "
                 f"({len(self.registered)} hotkeys)")
            return 0
        self.registered = hks
        self._reg_loaded_at = time.time()
        return len(hks)

    # ── connection reuse ─────────────────────────────────────────────────────
    # MEMORY: constructing a new bt.Subtensor per refresh leaks ~25 MB EACH TIME and
    # is NOT reclaimed by gc.collect() or by calling .close() — measured 2026-07-25
    # on this box (4 fresh instances: 123→148→173→202 MB; 4 reuses: 123→126 MB).
    # At REG_REFRESH_S=300 that is ~300 MB/h, which is exactly the ~290 MB/h that
    # took this process to 9.87 GB in 34h and made the kernel OOM-kill the SN89
    # hosted-miner multiplexer 10 times in 3 days. Hold ONE handle and reuse it.
    def _conn(self):
        if self._subtensor is None:
            import bittensor as bt
            self._subtensor = bt.Subtensor(NETWORK)
        return self._subtensor

    def _drop_conn(self) -> None:
        st, self._subtensor = self._subtensor, None
        try:
            if st is not None:
                st.close()
        except Exception:           # noqa: BLE001
            pass

    def prune(self) -> None:
        """Bound the two per-hotkey structures that otherwise grow forever.

        sent_ms  — check_rate() only reads submissions from the CURRENT UTC day
                   (daily cap) plus the single most recent timestamp (min gap), so
                   anything older than 48h is dead weight. Unpruned this grew one
                   int per accepted submission per hotkey, forever.
        lock_index — is_pair_locked() only looks back PAIR_LOCK_MS (24h), and
                   refresh_mech0_locks() re-adds live rows every LOCK_REFRESH_S, so
                   entries older than the lock window can never fire again.
        """
        now_ms = int(time.time() * 1000)
        keep_ms = now_ms - 48 * 3600 * 1000
        dropped = 0
        for hk, lst in list(self.sent_ms.items()):
            kept = [t for t in lst if int(t) >= keep_ms]
            dropped += len(lst) - len(kept)
            if kept:
                self.sent_ms[hk] = kept
            else:
                del self.sent_ms[hk]
        lock_cut = now_ms - hf.PAIR_LOCK_MS
        stale = [k for k, ts in self.lock_index.items() if int(ts) < lock_cut]
        for k in stale:
            del self.lock_index[k]
        if dropped or stale:
            _log(f"prune: sent_ms -{dropped} ts, lock_index -{len(stale)} keys "
                 f"(now {len(self.sent_ms)} hk / {len(self.lock_index)} locks)")
        self._pruned_at = time.time()

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

        # 2. registration — a hotkey with no UID cannot receive mecid-1 weight, so
        # accepting it only pollutes the anchored log and the public board. Checked
        # AFTER authenticity so the refusal is bound to a hotkey that really signed
        # it, and it is a SIGNED rejection like every other, so a miner who just
        # registered can tell "not on the subnet yet" from "dropped".
        if REQUIRE_REGISTERED and hk not in self.registered:
            return reject("not_registered")

        # 3. replay / equivocation — seq is strictly increasing per hotkey
        if seq <= self.last_seq.get(hk, -1):
            return reject("stale_seq")
        if abs(t_recv_ms - ts_miner) > hf.MAX_CLOCK_SKEW_MS:
            return reject("clock_skew")

        # 4. consensus validity — board, band, horizon, asset class
        t0 = t_recv_ms / 1000.0
        try:
            hf.validate_submission(payload, t0)
            hf.check_rate(self.sent_ms.get(hk, []), t_recv_ms, t0)
        except hf.HFRejected as e:
            return reject(str(e))

        # 5. the cross-mechanism pair lock
        pair = str(payload["trade_pair"]).upper()  # noqa: E501
        if hf.is_pair_locked(self.lock_index, hk, pair, hf.MECID, t_recv_ms):
            return reject("pair_locked_other_mechanism")

        # 6. accept — stamp, receipt, log
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
            if time.time() - self._pruned_at > 300:
                self.prune()
            # metagraph() blocks for seconds; off-loop or it stalls the whole
            # sub-second accept path on every refresh.
            if REQUIRE_REGISTERED and time.time() - self._reg_loaded_at > REG_REFRESH_S:
                await asyncio.to_thread(self.refresh_registered)
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
