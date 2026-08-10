#!/usr/bin/env python3
"""SN89 Signals miner.

Three modes:
  * CLI one-shot:   python neurons/miner.py submit --pair BTCUSD --direction LONG
  * REST intake:    python neurons/miner.py serve --port 8089 [--host 0.0.0.0]
                    POST /submit {"trade_pair": "BTCUSD", "direction": "LONG"}
                    (set SN89_INTAKE_TOKEN to require Authorization: Bearer …)
  * IQ follow:      python neurons/miner.py follow
                    Long-polls the IQ Signals feed for calls you submitted via
                    the Telegram Signals Bot / Chrome extension and auto-commits
                    them with YOUR local hotkey (non-custodial — keys never
                    leave this box). DM /token to the Signals Bot for a token,
                    then set SN89_FEED_TOKEN.
  * link X handle:  python neurons/miner.py register-x --handle @yourname
                    Signs a message with your hotkey and links your X (Twitter)
                    handle to it, so your handle shows on the public leaderboard
                    and we can tag you in social proof of your setups + earnings.
                    Only your hotkey signature is sent — never your keys.

All paths do the same thing (§4 of SPEC):
  1. build + validate the Signal (band/tp/sl come from the board file)
  2. dual-encrypt (tlock to T+2h round, owner X25519)
  3. upload the blob to your public bucket
  4. set_commitment(89) with the commitment hash + round + url tag

The commit BLOCK is your timestamp — your entry price is the open of the
first 1-second market bar at or after that block's on-chain timestamp, not
anything you claim. Submit means submit *now*.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import bittensor as bt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sn89_signals import bucket, chain, config, crypto, hf, sessions
from sn89_signals.schema import Signal, ValidationError, validate


def _fx_market_closed(now_utc: float | None = None) -> bool:
    """True when FX/metals spot markets are shut for the week.

    Delegates to sn89_signals.sessions so the miner's submit-time guard and the
    validator's dead-horizon void read ONE calendar. They were separate before
    (this function carried its own zoneinfo copy) and a miner whose tzdata
    disagreed with the validator's would have been rejected locally for a call
    the chain accepted, or the reverse."""
    return sessions.fx_market_closed(
        now_utc if now_utc is not None else time.time())


# ── local submission-limits guard (UX only — consensus enforcement is the
# validator's, per config.SUBMISSION_RULES_HISTORY). Without this a miner that
# fires over the daily cap or inside the min gap gets its commitment silently
# VOIDED at grading (daily_quota / min_spacing), which reads as "my signal
# disappeared". We track this box's own submits per hotkey and refuse locally
# with a clear message instead. Best-effort: only sees submits made through
# this tool on this box; the validator's judgment is authoritative.
def _submit_log_path(hotkey: str) -> str:
    return os.path.expanduser(f"~/.sn89/submits_{hotkey}.json")


def _load_submit_log(hotkey: str) -> list[float]:
    try:
        with open(_submit_log_path(hotkey), encoding="utf-8") as fh:
            return [float(t) for t in json.load(fh)]
    except (FileNotFoundError, ValueError, TypeError):
        return []


def record_local_submit(hotkey: str, now: float | None = None) -> None:
    now = now if now is not None else time.time()
    ts = _load_submit_log(hotkey)
    ts = sorted(t for t in ts if now - t < 2 * 86_400)[-49:] + [now]
    path = _submit_log_path(hotkey)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(ts, fh)


def check_local_limits(hotkey: str, now: float | None = None) -> "tuple[str, str] | None":
    """(kind, message) if a submit NOW would void under the rules in force
    (kind: 'quota' | 'gap'), else None. Era-aware via submission_rules_as_of,
    so the guard flips to the new limits exactly when consensus does."""
    now = now if now is not None else time.time()
    cap, gap = config.submission_rules_as_of(now)
    ts = _load_submit_log(hotkey)
    if gap and ts:
        since = now - max(ts)
        if since < gap:
            wait_min = int((gap - since) / 60) + 1
            return ("gap", f"min {gap // 60} min between calls — ~{wait_min} min to go "
                           f"(a commit now would VOID as min_spacing)")
    day = int(now // 86_400)
    today = sum(1 for t in ts if int(t // 86_400) == day)
    if today >= cap:
        return ("quota", f"daily cap reached ({cap}/UTC day) — a commit now would "
                         f"VOID as daily_quota; resets 00:00 UTC")
    return None


def build_signal(hotkey: str, pair: str, direction: str,
                 horizon_h: int | None = None, comment: str = "") -> Signal:
    bands = config.allowed_assets()
    band = bands.get(pair)
    if band is None:
        raise ValidationError(f"{pair} not on board; allowed: {sorted(bands.keys())}")
    _cls = band.get("asset_class", "")
    if _cls in sessions.SESSION_BOUND_CLASSES:
        _now = time.time()
        if _fx_market_closed(_now):
            raise ValidationError(
                f"{pair} rejected: FX & metals markets are closed for the weekend "
                f"(reopen Sun ~22:00 UTC / 17:00 New York). Crypto trades 24/7.")
        # Mirror the validator's dead-horizon void (config.FX_DEAD_HORIZON_FROM)
        # at submit time. Without this the call is accepted, costs a commit, and
        # is voided hours later with no explanation the trader ever sees.
        _hz = config.class_horizon_h(_cls)
        _open = sessions.open_fraction(_now, _hz)
        if _open < config.FX_MIN_OPEN_FRACTION:
            raise ValidationError(
                f"{pair} rejected: only {_open * 100:.0f}% of the {_hz}h grade window "
                f"falls in an open session (the FX week closes at 17:00 New York), "
                f"so the call could not resolve and would be voided. Wait for the "
                f"Sunday reopen, or trade crypto.")
    sig = Signal(
        trade_pair=pair,
        direction=direction.upper(),
        tp_bps=float(band["tp_bps"]),
        sl_bps=float(band["sl_bps"]),
        ts_miner=int(time.time() * 1000),
        hotkey=hotkey,
        asset_class=band.get("asset_class", ""),
        horizon_h=horizon_h or config.DEFAULT_HORIZON_H,
        comment=comment,
    )
    return validate(sig, bands)


def _push_owner_webhook(blob: dict, url: str) -> bool:
    """Optional realtime push to the subnet intake endpoint, if one is announced.

    Purely a latency optimization for subnet-side processing. The on-chain
    commitment remains the only canonical record — a push without a matching
    commitment is ignored.
    """
    hook = os.getenv("SN89_OWNER_WEBHOOK", "")
    if not hook:
        return False
    import requests
    try:
        r = requests.post(hook, json={"blob": blob, "url": url}, timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def submit(wallet: "bt.Wallet", sig: Signal, ch: chain.Chain | None = None) -> dict:
    ch = ch or chain.Chain()
    pt = sig.canonical_bytes()
    rnd = crypto.target_round(time.time() + config.REVEAL_DELAY_S)
    blob = crypto.encrypt(pt, sig.hotkey, rnd, config.OWNER_PK_HEX)
    url = bucket.upload(blob, sig.hotkey, sig.nonce)
    bucket.update_index(sig.hotkey, sig.nonce)
    # Real-time owner delivery, ON BY DEFAULT. Relay-hosted miners already
    # delivered the encrypted blob to the owner relay during upload; self-hosters
    # (own bucket / local disk) mirror a copy to the same relay intake so the
    # owner sees the submission live — their own URL stays canonical. A custom
    # SN89_OWNER_WEBHOOK, if set, is also pushed (override / extra sink).
    self_hosted = bool(bucket.BLOB_DIR) or bool(config.R2_ACCESS_KEY_ID)
    pushed = True if not self_hosted else bucket.mirror_to_relay(
        blob, sig.hotkey, sig.nonce)
    pushed = bool(pushed or _push_owner_webhook(blob, url))
    ok = ch.commit(wallet, sig.commitment().hex(), rnd, url)
    return {
        "ok": bool(ok),
        "nonce": sig.nonce,
        "commitment": sig.commitment().hex(),
        "round": rnd,
        "reveals_at_utc": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(crypto.round_time(rnd))),
        "blob_url": url,
        "pushed": pushed,  # owner has the blob in real time (relay or webhook)
    }



def _hf_seq_path(hotkey: str) -> str:
    return os.path.expanduser(f"~/.sn89/hf_seq_{hotkey}.json")


def _hf_next_seq(hotkey: str) -> int:
    """Strictly-increasing per-hotkey sequence — the ingest rejects a stale seq,
    so this must never go backwards. Persisted locally."""
    p = _hf_seq_path(hotkey)
    seq = 0
    try:
        with open(p, encoding="utf-8") as fh:
            seq = int(json.load(fh).get("seq", 0))
    except (OSError, ValueError):
        pass
    seq += 1
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"seq": seq}, fh)
    return seq


def submit_hf(wallet: "bt.Wallet", pair: str, direction: str) -> dict:
    """Submit one HF call to the ingest and return the signed receipt.

    Unlike LF (an on-chain commit), HF binds via a countersigned receipt over a
    persistent WSS connection — the receipt IS your proof we accepted the call.
    The board dictates the band and horizon; you cannot choose them.
    """
    import asyncio
    import secrets
    import websockets
    from bittensor_wallet import Keypair

    pair = pair.upper()
    direction = direction.upper()
    now = time.time()
    board = hf.hf_bands_as_of(now)
    if board is None or pair not in board:
        raise ValueError(
            f"{pair} is not on the HF board. HF pairs: {', '.join(sorted(board or {}))}")
    tp, sl, horizon_s, cls = board[pair]
    if direction not in ("LONG", "SHORT"):
        raise ValueError("direction must be LONG or SHORT")

    hk = wallet.hotkey.ss58_address
    seq = _hf_next_seq(hk)
    nonce = secrets.token_hex(16)
    ts_ms = int(now * 1000)
    payload = {"trade_pair": pair, "direction": direction, "asset_class": cls,
               "tp_bps": tp, "sl_bps": sl, "horizon_s": horizon_s}
    sb = hf.submit_signing_bytes(hk, seq, nonce, payload, ts_ms)
    frame = {"v": 1, "kind": "hf.submit", "hk": hk, "seq": seq, "nonce": nonce,
             "ts_miner": ts_ms, "payload": payload, "sig": wallet.hotkey.sign(sb).hex()}

    async def _send():
        async with websockets.connect(hf.HF_INGEST_WSS, open_timeout=20) as ws:
            await ws.send(json.dumps(frame))
            return json.loads(await asyncio.wait_for(ws.recv(), timeout=20))

    resp = asyncio.run(_send())

    if resp.get("kind") == "hf.receipt":
        rb = hf.receipt_signing_bytes(resp["hk"], resp["seq"], resp["ph"],
                                      resp["t_recv_us"], resp["grid_t0_ms"], resp["ing"])
        try:
            resp["verified"] = Keypair(ss58_address=hf.HF_RECEIPT_PUBKEY).verify(
                rb, bytes.fromhex(resp["sig_owner"]))
        except Exception:
            resp["verified"] = False
        # keep the receipt — it is the miner's proof of acceptance
        rp = os.path.expanduser(f"~/.sn89/hf_receipts_{hk}.jsonl")
        os.makedirs(os.path.dirname(rp), exist_ok=True)
        with open(rp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"submit": frame, "receipt": resp}) + "\n")
    return resp


def submit_closers(wallet: "bt.Wallet", position_id: str, action: str,
                   positions_url: str | None = None) -> dict:
    """Submit one Closers call (HOLD/CLOSE on a network position) and return
    the signed receipt. Same transport and receipt as HF — one more payload
    kind on the same ingest — so a self-hosted miner needs nothing new beyond
    this function.

    The positions feed (SN89_CLOSERS_POSITIONS_URL) is the board you vote on;
    pair and direction are read from YOUR chosen position and embedded in the
    payload, so the graded record is self-contained.
    """
    import asyncio
    import secrets
    import urllib.request
    import websockets
    from bittensor_wallet import Keypair
    from sn89_signals import closers

    action = action.upper()
    if action not in ("HOLD", "CLOSE"):
        raise ValueError("action must be HOLD or CLOSE")
    # Feed source, in order: an explicit URL (someone mirroring it themselves),
    # then SN89_CLOSERS_POSITIONS_URL, then the SAME authenticated endpoint the
    # `positions` subcommand uses. Without that last fallback this required an
    # env var with no public URL to point at, so the documented flow failed at
    # the vote step while step 1 worked (Canefis, 2026-08-05).
    url = positions_url or closers.POSITIONS_URL
    if url:
        with urllib.request.urlopen(url, timeout=10) as r:
            positions = json.loads(r.read().decode()).get("positions") or []
    else:
        positions = fetch_positions_feed(mint_miner_token(wallet))
    pos = next((p for p in positions
                if str(p.get("id")) == str(position_id)), None)
    if pos is None:
        ids = [str(p.get("id")) for p in positions][:10]
        hint = (", ".join(ids) if ids else
                "none right now — a position becomes votable only after it "
                "moves ±0.10% in position P&L")
        raise ValueError(f"unknown position {position_id!r}. Open now: {hint}")

    hk = wallet.hotkey.ss58_address
    seq = _hf_next_seq(hk)
    nonce = secrets.token_hex(16)
    ts_ms = int(time.time() * 1000)
    payload = {"kind": "closers", "position_id": str(position_id),
               "trade_pair": str(pos["trade_pair"]).upper(),
               "direction": str(pos["direction"]).upper(),
               "action": action, "asset_class": ""}
    sb = hf.submit_signing_bytes(hk, seq, nonce, payload, ts_ms)
    frame = {"v": 1, "kind": "hf.submit", "hk": hk, "seq": seq, "nonce": nonce,
             "ts_miner": ts_ms, "payload": payload,
             "sig": wallet.hotkey.sign(sb).hex()}

    async def _send():
        async with websockets.connect(hf.HF_INGEST_WSS, open_timeout=20) as ws:
            await ws.send(json.dumps(frame))
            return json.loads(await asyncio.wait_for(ws.recv(), timeout=20))

    resp = asyncio.run(_send())
    if resp.get("kind") == "hf.receipt":
        rb = hf.receipt_signing_bytes(resp["hk"], resp["seq"], resp["ph"],
                                      resp["t_recv_us"], resp["grid_t0_ms"], resp["ing"])
        try:
            resp["verified"] = Keypair(ss58_address=hf.HF_RECEIPT_PUBKEY).verify(
                rb, bytes.fromhex(resp["sig_owner"]))
        except Exception:  # noqa: BLE001
            resp["verified"] = False
        rp = os.path.expanduser(f"~/.sn89/hf_receipts_{hk}.jsonl")
        os.makedirs(os.path.dirname(rp), exist_ok=True)
        with open(rp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"submit": frame, "receipt": resp}) + "\n")
    return resp


# ── self-hosted Closers access: token, positions feed, local limit orders ────
PARTNER_API = os.getenv("SN89_PARTNER_API", "https://partner.infinitequant.app")


def mint_miner_token(wallet: "bt.Wallet") -> str:
    """Prove hotkey ownership (challenge → local sign → verify) and return an
    sn89 miner token. Keys never leave this box; the token only READS (feed,
    stream) and is revocable server-side."""
    import requests
    hk = wallet.hotkey.ss58_address
    ch = requests.post(f"{PARTNER_API}/api/sn89/register/challenge",
                       json={"hotkey": hk}, timeout=10).json()
    msg = ch.get("message") or ch.get("nonce")
    if not msg:
        raise RuntimeError(f"challenge failed: {ch}")
    sig = wallet.hotkey.sign(msg.encode() if isinstance(msg, str) else msg).hex()
    v = requests.post(f"{PARTNER_API}/api/sn89/register/verify",
                      json={"hotkey": hk, "nonce": ch.get("nonce", msg),
                            "signature": sig}, timeout=10).json()
    if not v.get("token"):
        raise RuntimeError(f"verify failed: {v}")
    return v["token"]


def fetch_positions_feed(token: str) -> list[dict]:
    import requests
    r = requests.get(f"{PARTNER_API}/api/sn89/closers/positions",
                     headers={"Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    return r.json().get("positions") or []


def cmd_transfer_referrals(args) -> int:
    """ONE-TIME referral-base transfer (sn89refx). Signed by THIS wallet's
    hotkey — the ORIGINAL recruiter — it hands every referral this hotkey ever
    committed to the destination hotkey, forever. The bridge for referrers who
    built their base on partner traders' hotkeys before the referrer mechanism
    existed: the trader runs this once, the base lands on the referrer's own
    miner.

    Protocol facts, not preferences:
      * only the EARLIEST transfer from this hotkey ever counts — running it
        again, or a second transfer to someone else, is permanently inert;
      * non-chaining: the destination cannot re-transfer what it received;
      * this hotkey's referrer score drops to zero the moment it lands.
    """
    w = _wallet(args)
    to = args.to.strip()
    if to == w.hotkey.ss58_address:
        print("REFUSED: destination is this same hotkey", file=sys.stderr)
        return 1
    print(f"About to transfer THIS HOTKEY'S ENTIRE REFERRAL BASE, one time, "
          f"irreversibly:\n  from  {w.hotkey.ss58_address}\n  to    {to}\n")
    if input("Type the destination's last 6 characters to confirm: ").strip() != to[-6:]:
        print("confirmation mismatch — nothing committed", file=sys.stderr)
        return 1
    ch = chain.Chain()
    ok = ch.commit_referral_transfer(w, to)
    print("transfer committed on-chain — it takes effect at the validator's "
          "next weight cycle" if ok else "COMMIT FAILED — nothing transferred")
    return 0 if ok else 1


def cmd_positions(args) -> int:
    """One-shot: print the network's open positions (the Closers board)."""
    token = mint_miner_token(_wallet(args))
    print(json.dumps(fetch_positions_feed(token), indent=2))
    return 0


def cmd_watch_positions(args) -> int:
    """Hold the SSE stream open and print one JSON line per feed CHANGE —
    pipe this into your algo for push alerts on new/closed positions:

        python neurons/miner.py ... watch-positions | your_algo
    """
    import requests
    token = mint_miner_token(_wallet(args))
    url = f"{PARTNER_API}/api/sn89/closers/positions/stream"
    while True:
        try:
            with requests.get(url, headers={"Authorization": f"Bearer {token}"},
                              stream=True, timeout=(10, 90)) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if line and line.startswith(b"data:"):
                        print(line[5:].decode().strip(), flush=True)
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001 — reconnect; snapshot replays
            print(f"# stream reconnect: {e}", file=sys.stderr)
            time.sleep(3)


def cmd_limit(args) -> int:
    """Self-hosted LIMIT submission — HF, LF or Closers. Watches the live
    quote stream (our tick fan-out) and, the moment price crosses the trigger,
    signs and submits FROM THIS BOX with your keys:

        limit --kind hf --pair XAUUSD --direction LONG --trigger 4020
        limit --kind lf --pair BTCUSD --direction SHORT --trigger 63500
        limit --kind closers --position-id <id> --action CLOSE --trigger 62500

    Side is inferred: trigger above the current price fires on >=, below on <=.
    The order rests only while this process runs (ctrl-C cancels — nothing is
    parked server-side, and your keys never leave the box).
    """
    import requests
    w = _wallet(args)
    kind = args.kind
    token = mint_miner_token(w)
    if kind == "closers":
        pos = {str(p["id"]): p for p in fetch_positions_feed(token)}
        p = pos.get(args.position_id)
        if p is None:
            print(f"unknown position {args.position_id}; open: {list(pos)[:10]}",
                  file=sys.stderr)
            return 1
        pair = str(p["trade_pair"]).upper()
    else:
        pair = args.pair.upper()
    trigger = float(args.trigger)
    url = f"{PARTNER_API}/api/sn89/closers/stream?pair={pair}"
    side = None
    print(f"resting {kind} limit: {pair} @ {trigger} — watching live quotes…",
          flush=True)
    while True:
        try:
            with requests.get(url, headers={"Authorization": f"Bearer {token}"},
                              stream=True, timeout=(10, 90)) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line or not line.startswith(b"data:"):
                        continue
                    d = json.loads(line[5:])
                    px_now = d.get("price")
                    if not px_now:
                        continue
                    if side is None:
                        side = "above" if trigger > px_now else "below"
                        print(f"  fires when price {'>=' if side == 'above' else '<='} "
                              f"{trigger} (now {px_now})", flush=True)
                    hit = (px_now >= trigger) if side == "above" else (px_now <= trigger)
                    if not hit:
                        continue
                    print(f"  triggered @ {px_now} — submitting…", flush=True)
                    if kind == "hf":
                        resp = submit_hf(w, pair, args.direction)
                    elif kind == "closers":
                        resp = submit_closers(w, args.position_id, args.action)
                    else:
                        sig = build_signal(w.hotkey.ss58_address, pair,
                                           args.direction.upper(), comment="iq-limit")
                        resp = submit(w, sig)
                    print(json.dumps(resp, indent=2))
                    return 0
        except KeyboardInterrupt:
            print("cancelled — nothing was submitted")
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"# stream reconnect: {e}", file=sys.stderr)
            time.sleep(3)


def cmd_submit_closers(args) -> int:
    w = _wallet(args)
    try:
        resp = submit_closers(w, args.position_id, args.action)
    except ValueError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"SUBMIT FAILED: {e}", file=sys.stderr)
        return 1
    print(json.dumps(resp, indent=2))
    if resp.get("kind") == "hf.receipt":
        return 0
    print(f"REFUSED: {resp.get('reason', 'unknown')}", file=sys.stderr)
    return 1


def cmd_submit_hf(args) -> int:
    w = _wallet(args)
    try:
        resp = submit_hf(w, args.pair, args.direction)
    except ValueError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"SUBMIT FAILED: {e}", file=sys.stderr)
        return 1
    print(json.dumps(resp, indent=2))
    if resp.get("kind") == "hf.receipt":
        if not resp.get("verified"):
            print("WARNING: receipt signature did NOT verify against the known "
                  "ingest key — keep it anyway and flag the operator.", file=sys.stderr)
        return 0
    print(f"REFUSED: {resp.get('reason', 'unknown')}", file=sys.stderr)
    return 1


def _wallet(args) -> "bt.Wallet":
    return bt.Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey)


def cmd_submit(args) -> int:
    w = _wallet(args)
    limit = check_local_limits(w.hotkey.ss58_address)
    if limit:
        print(f"REFUSED ({limit[0]}): {limit[1]}", file=sys.stderr)
        return 1
    try:
        sig = build_signal(w.hotkey.ss58_address, args.pair, args.direction,
                           args.horizon, args.comment or "")
    except ValidationError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1
    res = submit(w, sig)
    if res["ok"]:
        record_local_submit(w.hotkey.ss58_address)
    print(json.dumps(res, indent=2))
    return 0 if res["ok"] else 1


def cmd_serve(args) -> int:
    from http.server import BaseHTTPRequestHandler, HTTPServer

    w = _wallet(args)
    hotkey = w.hotkey.ss58_address
    ch = chain.Chain()
    token = os.getenv("SN89_INTAKE_TOKEN", "")
    if args.host != "127.0.0.1" and not token:
        print("REFUSING to bind a public interface without SN89_INTAKE_TOKEN set "
              "— anyone could burn your 6 daily submissions.", file=sys.stderr)
        return 1

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/submit":
                return self._reply(404, {"error": "POST /submit"})
            if token and self.headers.get("Authorization", "") != f"Bearer {token}":
                return self._reply(401, {"error": "bad or missing bearer token"})
            try:
                limit = check_local_limits(hotkey)
                if limit:
                    return self._reply(429, {"error": limit[1], "kind": limit[0]})
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                sig = build_signal(hotkey, body["trade_pair"], body["direction"],
                                   body.get("horizon_h"), body.get("comment", ""))
                res = submit(w, sig, ch)
                if res["ok"]:
                    record_local_submit(hotkey)
                self._reply(200 if res["ok"] else 502, res)
            except ValidationError as e:
                self._reply(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                self._reply(500, {"error": str(e)})

        def _reply(self, code: int, obj: dict):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, fmt, *a):  # quiet
            pass

    auth = "bearer-auth" if token else "no auth (localhost only)"
    print(f"SN89 miner REST intake on {args.host}:{args.port} (hotkey {hotkey}, {auth})")
    HTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


def cmd_follow(args) -> int:
    """Mirror your own IQ Signals calls (Telegram bot / Chrome extension) onto
    SN89 with your local hotkey.

    Non-custodial: the IQ platform only tells you WHAT you submitted; the
    signing wallet, the timelock encryption, and the on-chain commitment all
    happen here. The feed is token-scoped to your own calls — you can never
    see (or mine) another trader's signal.
    """
    import requests

    w = _wallet(args)
    hotkey = w.hotkey.ss58_address
    ch = chain.Chain()
    feed = args.feed or os.getenv("SN89_FEED_URL",
                                  "https://partner.infinitequant.app/api/sn89/feed")
    token = os.getenv("SN89_FEED_TOKEN", "")
    if not token:
        print("SN89_FEED_TOKEN not set — DM /token to the IQ Signals Bot to get one.",
              file=sys.stderr)
        return 1

    state_path = os.getenv("SN89_FOLLOW_STATE") or os.path.expanduser("~/.sn89/follow_state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (FileNotFoundError, ValueError):
        state = {"last_id": 0}

    print(f"SN89 follow: {feed} → hotkey {hotkey} (after id {state['last_id']}, "
          f"poll {args.interval}s)")
    while True:
        try:
            r = requests.get(feed, params={"after_id": state["last_id"]},
                             headers={"Authorization": f"Bearer {token}"}, timeout=10)
            if r.status_code == 401:
                print("feed: token rejected — re-issue with /token in the Signals Bot",
                      file=sys.stderr)
                return 1
            calls = r.json().get("signals", []) if r.status_code == 200 else []
            for c in sorted(calls, key=lambda x: x["id"]):
                limit = check_local_limits(hotkey)
                if limit and limit[0] == "gap":
                    # hold — the commit lands cleanly once the gap has passed
                    print(f"  ⏸ #{c['id']} held: {limit[1]} — retrying next poll")
                    break
                if limit:  # quota — committing later (after 00:00) would be stale; drop
                    print(f"  ✗ #{c['id']} dropped: {limit[1]}")
                    state["last_id"] = max(state["last_id"], c["id"])
                    with open(state_path, "w", encoding="utf-8") as fh:
                        json.dump(state, fh)
                    continue
                try:
                    sig = build_signal(hotkey, c["asset"], c["direction"],
                                       c.get("horizon_hours"),
                                       f"iq-follow:{c['id']}")
                    res = submit(w, sig, ch)
                    if res["ok"]:
                        record_local_submit(hotkey)
                    print(f"  ↗ #{c['id']} {c['direction']} {c['asset']} → "
                          f"commit {res['commitment'][:12]}… ok={res['ok']}")
                except ValidationError as e:
                    print(f"  ✗ #{c['id']} skipped: {e}")
                except Exception as e:  # noqa: BLE001 — keep following on transient errors
                    print(f"  ✗ #{c['id']} error: {e}", file=sys.stderr)
                    break  # retry this id next poll
                else:
                    state["last_id"] = max(state["last_id"], c["id"])
                    with open(state_path, "w", encoding="utf-8") as fh:
                        json.dump(state, fh)
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"  feed poll error: {e}", file=sys.stderr)
        time.sleep(args.interval)


_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def normalize_handle(raw: str) -> str:
    """Accept '@name', 'name', or any x.com/twitter.com profile URL → 'name'."""
    h = (raw or "").strip()
    for pre in ("https://x.com/", "https://twitter.com/", "http://x.com/",
                "http://twitter.com/", "x.com/", "twitter.com/", "@"):
        if h.lower().startswith(pre):
            h = h[len(pre):]
            break
    return h.strip("/").split("?")[0].split("/")[0]


def cmd_register_x(args) -> int:
    """Link an X (Twitter) handle to this hotkey, signed by the hotkey.

    The handle is the only thing published; we send {hotkey, handle, ts,
    signature} — the signature proves you control the hotkey. The same canonical
    message ``sn89-register-x:<hotkey>:<handle>:<ts>`` is re-derived and verified
    server-side; nothing but the signature ever leaves this box.
    """
    import requests

    w = _wallet(args)
    hotkey = w.hotkey.ss58_address
    handle = normalize_handle(args.handle)
    if not _HANDLE_RE.match(handle):
        print(f"INVALID handle '{args.handle}' — X handles are 1–15 characters "
              f"(letters, digits, underscore).", file=sys.stderr)
        return 1

    ts = int(time.time())
    msg = f"sn89-register-x:{hotkey}:{handle}:{ts}"
    signature = w.hotkey.sign(msg.encode()).hex()
    url = args.url or os.getenv(
        "SN89_REGISTER_URL", "https://partner.infinitequant.app/api/sn89/register-x")
    try:
        r = requests.post(url, json={"hotkey": hotkey, "handle": handle,
                                     "ts": ts, "signature": signature}, timeout=10)
    except Exception as e:  # noqa: BLE001
        print(f"register-x: could not reach {url}: {e}", file=sys.stderr)
        return 1
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text}
    if r.status_code == 200 and body.get("ok"):
        print(f"✓ linked @{handle} to hotkey {hotkey}")
        print("  It appears on the SN89 leaderboard at the next refresh "
              "(infinitequant.app).")
        return 0
    print(f"register-x failed [{r.status_code}]: {body.get('error') or body}",
          file=sys.stderr)
    return 1


def cmd_refer(args) -> int:
    """Commit a referral claim for a NEW hotkey (§ referral incentive).

    Run this BEFORE the recruit registers on netuid 89 — the claim is only
    valid if your commitment lands at least REFERRAL_MIN_LEAD_BLOCKS (~2 min)
    before the recruit's registration block. Sequencing guards (CommitmentOf is
    one latest-wins slot per hotkey, and the validator polls every ~30s):
      * refuses if YOUR last commitment landed < ~90s ago (it may not have been
        observed yet — a referral now could shadow it);
      * hold your next signal ~90s after this, or it could shadow the referral;
      * register the recruit only AFTER the referral shows in the public
        checkpoint (republished every ~5 min) — that is your confirmation.
    """
    w = _wallet(args)
    hotkey = w.hotkey.ss58_address
    recruit = (args.recruit or "").strip()
    if chain.decode_referral(chain.encode_referral(recruit)) is None:
        print(f"INVALID recruit address '{recruit}' — not a checksum-valid ss58 "
              f"hotkey.", file=sys.stderr)
        return 1
    if recruit == hotkey:
        print("INVALID: you cannot refer your own hotkey.", file=sys.stderr)
        return 1
    ch = chain.Chain()
    if ch.uid_of(recruit) is not None:
        print(f"REFUSING: {recruit} is ALREADY registered on netuid "
              f"{config.NETUID} — a referral must be committed BEFORE the "
              f"recruit registers, so this claim would be permanently invalid.",
              file=sys.stderr)
        return 1
    cur_block = ch.current_block()
    mine = ch.read_all_commitments_with_block().get(hotkey)
    guard_blocks = 8    # ~96s at 12s blocks ≈ 3 validator polls
    if mine and cur_block - int(mine.get("commit_block") or 0) < guard_blocks:
        wait_s = (guard_blocks - (cur_block - int(mine["commit_block"]))) * 12
        print(f"REFUSING: your last commitment landed {cur_block - mine['commit_block']} "
              f"block(s) ago and may not be observed yet — a referral now could "
              f"shadow it. Retry in ~{wait_s}s.", file=sys.stderr)
        return 1
    ok = ch.commit_referral(w, recruit)
    print(json.dumps({"ok": bool(ok), "recruiter": hotkey, "recruit": recruit}, indent=2))
    if ok:
        print("\nNEXT STEPS:\n"
              "  1. Hold your next SIGNAL commit for ~90s (it could shadow this "
              "referral before the validator observes it).\n"
              "  2. Wait until this referral appears in the public checkpoint "
              "(republished every ~5 min) — that confirms it was journaled.\n"
              "  3. ONLY THEN register the recruit hotkey on netuid 89.\n"
              "The bonus starts once BOTH hotkeys are earning; strict no-copy "
              "applies inside the pair.", file=sys.stderr)
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description="SN89 Signals miner")
    p.add_argument("--wallet.name", dest="wallet_name", default="default")
    p.add_argument("--wallet.hotkey", dest="wallet_hotkey", default="default")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("submit", help="submit one signal now")
    ps.add_argument("--pair", required=True)
    ps.add_argument("--direction", required=True, choices=["LONG", "SHORT", "long", "short"])
    ps.add_argument("--horizon", type=int, default=None)
    ps.add_argument("--comment", default="")
    ps.set_defaults(fn=cmd_submit)

    ph = sub.add_parser("submit-hf",
                        help="submit one HIGH-FREQUENCY signal now (mecid 1)")
    ph.add_argument("--pair", required=True)
    ph.add_argument("--direction", required=True,
                    choices=["LONG", "SHORT", "long", "short"])
    ph.set_defaults(fn=cmd_submit_hf)

    pc = sub.add_parser("submit-closers",
                        help="vote HOLD/CLOSE on a network open position (Closers)")
    pc.add_argument("position_id", help="position id from the positions feed")
    pc.add_argument("action", choices=["HOLD", "CLOSE", "hold", "close"])
    pc.set_defaults(fn=cmd_submit_closers)

    pp = sub.add_parser("positions", help="print the Closers open-positions board")
    pp.set_defaults(fn=cmd_positions)

    pw = sub.add_parser("watch-positions",
                        help="SSE-follow the positions feed; one JSON line per change")
    pw.set_defaults(fn=cmd_watch_positions)

    pl = sub.add_parser("limit",
                        help="rest a local limit submission (hf/lf/closers); fires on trigger")
    pl.add_argument("--kind", choices=["hf", "lf", "closers"], required=True)
    pl.add_argument("--pair", help="board pair (hf/lf)")
    pl.add_argument("--direction", choices=["LONG", "SHORT", "long", "short"],
                    help="hf/lf direction")
    pl.add_argument("--position-id", dest="position_id", help="closers position id")
    pl.add_argument("--action", choices=["HOLD", "CLOSE", "hold", "close"],
                    help="closers action")
    pl.add_argument("--trigger", required=True, help="trigger price")
    pl.set_defaults(fn=cmd_limit)

    pv = sub.add_parser("serve", help="run REST intake")
    pv.add_argument("--port", type=int, default=8089)
    pv.add_argument("--host", default="127.0.0.1",
                    help="bind address; non-localhost requires SN89_INTAKE_TOKEN")
    pv.set_defaults(fn=cmd_serve)

    pf = sub.add_parser("follow", help="mirror your IQ Signals bot/extension calls")
    pf.add_argument("--feed", default=None,
                    help="feed URL (default SN89_FEED_URL or the IQ endpoint)")
    pf.add_argument("--interval", type=int, default=5, help="poll seconds")
    pf.set_defaults(fn=cmd_follow)

    px = sub.add_parser("register-x",
                        help="link your X (Twitter) handle to your hotkey "
                             "for the leaderboard + social proof")
    px.add_argument("--handle", required=True,
                    help="your X handle, e.g. @yourname (or a full profile URL)")
    px.add_argument("--url", default=None,
                    help="override the registration endpoint (default SN89_REGISTER_URL "
                         "or the IQ endpoint)")
    px.set_defaults(fn=cmd_register_x)

    pr = sub.add_parser("refer",
                        help="commit a referral claim for a NEW hotkey BEFORE it "
                             "registers (both earn a bonus once both are earning)")
    pr.add_argument("recruit", help="the recruit's hotkey ss58 (NOT yet registered)")
    pr.set_defaults(fn=cmd_refer)

    pt = sub.add_parser("transfer-referrals",
                        help="ONE-TIME: move every referral this hotkey committed "
                             "onto another hotkey (the referrer's own miner)")
    pt.add_argument("--to", required=True, help="destination hotkey ss58")
    pt.set_defaults(fn=cmd_transfer_referrals)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
