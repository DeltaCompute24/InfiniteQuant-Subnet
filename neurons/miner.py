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
                    leave this box). DM /miner to the Signals Bot for a token,
                    then set SN89_FEED_TOKEN.

All paths do the same thing (§4 of SPEC):
  1. build + validate the Signal (band/tp/sl come from the board file)
  2. dual-encrypt (tlock to T+24h round, owner X25519)
  3. upload the blob to your public bucket
  4. set_commitment(89) with the commitment hash + round + url tag

The commit BLOCK is your timestamp — your entry price is the open of the
first 1-second market bar ~30s after that block's on-chain timestamp, not
anything you claim. Submit means submit *now*.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import bittensor as bt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sn89_signals import bucket, chain, config, crypto
from sn89_signals.schema import Signal, ValidationError, validate


def build_signal(hotkey: str, pair: str, direction: str,
                 horizon_h: int | None = None, comment: str = "") -> Signal:
    bands = config.allowed_assets()
    band = bands.get(pair)
    if band is None:
        raise ValidationError(f"{pair} not on board; allowed: {sorted(bands.keys())}")
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
    pushed = _push_owner_webhook(blob, url)
    ok = ch.commit(wallet, sig.commitment().hex(), rnd, url)
    return {
        "ok": bool(ok),
        "nonce": sig.nonce,
        "commitment": sig.commitment().hex(),
        "round": rnd,
        "reveals_at_utc": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(crypto.round_time(rnd))),
        "blob_url": url,
        "pushed": pushed,
    }


def _wallet(args) -> "bt.Wallet":
    return bt.Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey)


def cmd_submit(args) -> int:
    w = _wallet(args)
    try:
        sig = build_signal(w.hotkey.ss58_address, args.pair, args.direction,
                           args.horizon, args.comment or "")
    except ValidationError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1
    res = submit(w, sig)
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
              "— anyone could burn your 3 daily submissions.", file=sys.stderr)
        return 1

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/submit":
                return self._reply(404, {"error": "POST /submit"})
            if token and self.headers.get("Authorization", "") != f"Bearer {token}":
                return self._reply(401, {"error": "bad or missing bearer token"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                sig = build_signal(hotkey, body["trade_pair"], body["direction"],
                                   body.get("horizon_h"), body.get("comment", ""))
                res = submit(w, sig, ch)
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
        print("SN89_FEED_TOKEN not set — DM /miner to the IQ Signals Bot to get one.",
              file=sys.stderr)
        return 1

    state_path = os.path.expanduser("~/.sn89/follow_state.json")
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
                print("feed: token rejected — re-issue with /miner in the Signals Bot",
                      file=sys.stderr)
                return 1
            calls = r.json().get("signals", []) if r.status_code == 200 else []
            for c in sorted(calls, key=lambda x: x["id"]):
                try:
                    sig = build_signal(hotkey, c["asset"], c["direction"],
                                       c.get("horizon_hours"),
                                       f"iq-follow:{c['id']}")
                    res = submit(w, sig, ch)
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

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
