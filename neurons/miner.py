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

from sn89_signals import bucket, chain, config, crypto
from sn89_signals.schema import Signal, ValidationError, validate


def _fx_market_closed(now_utc: float | None = None) -> bool:
    """True when FX/metals spot markets are shut for the week. The retail FX week
    runs Sun 17:00 -> Fri 17:00 America/New_York; anchoring on NY local time makes
    the boundary DST-correct (= 22:00 UTC winter / 21:00 UTC summer)."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    dt = datetime.fromtimestamp(
        now_utc if now_utc is not None else time.time(), tz=timezone.utc
    ).astimezone(ZoneInfo("America/New_York"))
    wd, hour = dt.weekday(), dt.hour   # Mon=0 .. Sun=6
    if wd == 5:                         # Saturday
        return True
    if wd == 4 and hour >= 17:          # Friday 17:00+ NY
        return True
    if wd == 6 and hour < 17:           # Sunday before 17:00 NY
        return True
    return False


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
    if _cls in ("forex", "forex-commodities") and _fx_market_closed():
        raise ValidationError(
            f"{pair} rejected: FX & metals markets are closed for the weekend "
            f"(reopen Sun ~22:00 UTC / 17:00 New York). Crypto trades 24/7.")
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

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
