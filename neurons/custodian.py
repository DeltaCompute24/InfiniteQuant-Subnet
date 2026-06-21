#!/usr/bin/env python3
"""SN89 collateral custodian — owner-side service + CLI (docs/collateral.md).

The custodian holds the two privileged keys (vault coldkey + EVM ledger owner
key) and executes every custody event. Validators never run this; they only
read the ledger. Miners interact through `serve` (the self-serve HTTP API that
neurons/collateral_cli.py talks to) — every mutating request is authorized by
the caller's own coldkey signature, never by an account.

Owner-side operations:
  serve                   run the collateral HTTP API (deposit / withdraw /
                          query-withdraw / balance / info)
  deposit                 process one deposit extrinsic from a file/arg
  withdraw                process one signed withdraw-request JSON file
  slash-eliminated        burn collateral of hotkeys the validator journal
                          marked eliminated (dry-run by default)
  balance                 read the public ledger

Env (owner): SN89_COLLATERAL_CONTRACT, SN89_VAULT_COLDKEY,
SN89_OWNER_EVM_ADDRESS, SN89_OWNER_EVM_KEY, SN89_VAULT_WALLET (wallet name),
SN89_CUSTODIAN_DB (nonce journal, default ~/.sn89/custodian.db).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sn89_signals import collateral, config

MAX_BODY_BYTES = 256_000          # extrinsic hex for one transfer_stake is ~1 KB
CUSTODIAN_DB = os.getenv("SN89_CUSTODIAN_DB",
                         os.path.expanduser("~/.sn89/custodian.db"))

_chain_lock = threading.Lock()    # one chain mutation at a time
_st_cached = None


def _st():
    global _st_cached
    if _st_cached is None:
        import bittensor as bt
        _st_cached = bt.Subtensor(network=config.NETWORK)
    return _st_cached


def _owner_evm() -> tuple[str, str]:
    addr, key = os.getenv("SN89_OWNER_EVM_ADDRESS", ""), os.getenv("SN89_OWNER_EVM_KEY", "")
    if not addr or not key:
        sys.exit("set SN89_OWNER_EVM_ADDRESS and SN89_OWNER_EVM_KEY")
    return addr, key


def _vault_wallet():
    name = os.getenv("SN89_VAULT_WALLET", "")
    if not name:
        sys.exit("set SN89_VAULT_WALLET (vault wallet name)")
    import bittensor as bt
    return bt.Wallet(name=name)


def _cdb() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(CUSTODIAN_DB), exist_ok=True)
    db = sqlite3.connect(CUSTODIAN_DB)
    db.execute("CREATE TABLE IF NOT EXISTS used_nonces "
               "(k TEXT PRIMARY KEY, ts REAL NOT NULL)")
    return db


def consume_nonce(db: sqlite3.Connection, coldkey: str, hotkey: str,
                  nonce: str) -> bool:
    """Record a withdraw nonce; False if it was already used (replay)."""
    try:
        db.execute("INSERT INTO used_nonces (k, ts) VALUES (?, ?)",
                   (f"{coldkey}::{hotkey}::{nonce}", time.time()))
        db.commit()
        return True
    except sqlite3.IntegrityError:
        return False


# ── core ops (shared by CLI commands and the HTTP API) ────────────────────────
# Rejections raise ValueError (HTTP 400); infrastructure failures raise
# RuntimeError (HTTP 502 — nothing was mutated unless the message says so).

def process_deposit(extrinsic_hex: str) -> dict:
    """Validate + submit a caller-signed deposit extrinsic, credit the ledger.
    Returns {hotkey, credited_rao, ledger_tx}."""
    st = _st()
    try:
        ext = collateral.decode_extrinsic(st, extrinsic_hex)
        hotkey, amount = collateral.validate_deposit_extrinsic(
            ext, config.VAULT_COLDKEY, config.NETUID)
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001 — scale decode errors etc.
        raise ValueError(f"undecodable extrinsic: {e}") from e
    addr, key = _owner_evm()
    with _chain_lock:
        receipt = st.substrate.submit_extrinsic(ext, wait_for_inclusion=True)
        if not receipt.is_success:
            raise ValueError(f"transfer_stake failed: {receipt.error_message}")
        credited = collateral.stake_added_rao(
            [e.value if hasattr(e, "value") else e
             for e in receipt.triggered_events])
        tx = collateral.ledger_write("deposit", hotkey, credited, addr, key)
    print(f"  deposit {credited / collateral.RAO_PER_ALPHA:.4f} alpha "
          f"from {hotkey[:8]}… · ledger {tx}")
    return {"hotkey": hotkey, "credited_rao": credited, "ledger_tx": tx}


def withdraw_preview(hotkey: str, validator_db_path: str | None = None) -> dict:
    """Withdrawal eligibility for a hotkey, from the validator journal +
    ledger. Pure read — also serves the query-withdraw endpoint."""
    db = sqlite3.connect(validator_db_path or config.DB_PATH)
    row = db.execute("SELECT eliminated_t0 FROM hotkey_meta WHERE hotkey=?",
                     (hotkey,)).fetchone()
    eliminated = bool(row and row[0] is not None)
    open_n = db.execute(
        "SELECT COUNT(*) FROM signals WHERE hotkey=? AND status IN "
        "('sealed','revealed','pending')", (hotkey,)).fetchone()[0]
    last_t0 = db.execute("SELECT MAX(t0_unix) FROM signals WHERE hotkey=?",
                         (hotkey,)).fetchone()[0]
    cooldown_left = max(0.0, (last_t0 or 0) + config.WITHDRAW_COOLDOWN_S - time.time())
    balance = collateral.CollateralLedger().balance_of(hotkey)
    reasons = []
    if eliminated:
        reasons.append("eliminated — collateral burns")
    if open_n:
        reasons.append(f"{open_n} unsettled signal(s)")
    if cooldown_left > 0:
        reasons.append(f"cooldown: {cooldown_left / 3600:.1f}h remaining")
    return {"hotkey": hotkey, "balance_rao": balance, "eligible": not reasons,
            "reasons": reasons}


def process_withdraw(req: dict) -> dict:
    """Verify a coldkey-signed withdraw request end-to-end and execute it.
    Returns {hotkey, amount_rao, ledger_tx}."""
    err = collateral.verify_withdraw_request(req)
    if err:
        raise ValueError(err)
    hotkey, coldkey, amount = req["hotkey"], req["coldkey"], int(req["amount_rao"])

    if not consume_nonce(_cdb(), coldkey, hotkey, str(req["nonce"])):
        raise ValueError("nonce already used")

    st = _st()
    owner = st.substrate.query("SubtensorModule", "Owner", [hotkey])
    if str(getattr(owner, "value", owner)) != coldkey:
        raise ValueError(f"{coldkey} does not own {hotkey}")

    preview = withdraw_preview(hotkey)
    if not preview["eligible"]:
        raise ValueError("; ".join(preview["reasons"]))
    if preview["balance_rao"] < amount:
        raise ValueError("amount exceeds posted collateral")

    addr, key = _owner_evm()
    with _chain_lock:
        tx = collateral.ledger_write("withdraw", hotkey, amount, addr, key)
        ext = collateral.create_stake_transfer_extrinsic(  # vault → caller
            st, _vault_wallet(), hotkey, amount, dest_coldkey=coldkey)
        receipt = st.substrate.submit_extrinsic(ext, wait_for_inclusion=True)
        if not receipt.is_success:
            # put the ledger back so books match the vault
            collateral.ledger_write("deposit", hotkey, amount, addr, key)
            raise RuntimeError(
                f"transfer_stake back failed (ledger restored): {receipt.error_message}")
    print(f"  withdraw {amount / collateral.RAO_PER_ALPHA:.4f} alpha "
          f"to {coldkey[:8]}… · ledger {tx}")
    return {"hotkey": hotkey, "amount_rao": amount, "ledger_tx": tx}


# ── HTTP API (self-serve transport — neurons/collateral_cli.py is the client) ─
class _Handler(BaseHTTPRequestHandler):
    server_version = "sn89-custodian"

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY_BYTES:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def log_message(self, fmt, *args):  # quiet default access log
        pass

    def do_GET(self):
        parts = self.path.rstrip("/").split("/")
        if self.path.rstrip("/") == "/collateral/info":
            return self._json(200, {
                "netuid": config.NETUID,
                "network": config.NETWORK,
                "vault_coldkey": config.VAULT_COLDKEY,
                "contract": config.COLLATERAL_CONTRACT,
                "evm_endpoint": config.EVM_ENDPOINT,
                "min_collateral_alpha": config.COLLATERAL_MIN_ALPHA,
            })
        if len(parts) == 4 and parts[1] == "collateral" and parts[2] == "balance":
            try:
                rao = collateral.CollateralLedger().balance_of(parts[3])
                return self._json(200, {"hotkey": parts[3], "balance_rao": rao})
            except Exception as e:  # noqa: BLE001
                return self._json(502, {"error": str(e)})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        route = self.path.rstrip("/")
        try:
            body = self._body()
            if route == "/collateral/deposit":
                return self._json(200, process_deposit(str(body["extrinsic_hex"])))
            if route == "/collateral/query-withdraw":
                return self._json(200, withdraw_preview(str(body["hotkey"])))
            if route == "/collateral/withdraw":
                return self._json(200, process_withdraw(body))
            return self._json(404, {"error": "not found"})
        except (ValueError, KeyError) as e:
            return self._json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            print(f"  ! {route}: {e}")
            return self._json(502, {"error": str(e)})


def cmd_serve(a):
    _owner_evm()        # fail fast on missing keys
    _vault_wallet()
    if not config.VAULT_COLDKEY or not config.COLLATERAL_CONTRACT:
        sys.exit("set SN89_VAULT_COLDKEY and SN89_COLLATERAL_CONTRACT")
    srv = ThreadingHTTPServer((a.host, a.port), _Handler)
    print(f"collateral API on http://{a.host}:{a.port} · netuid={config.NETUID} "
          f"· vault={config.VAULT_COLDKEY[:8]}… (terminate TLS in front of this)")
    srv.serve_forever()


# ── CLI wrappers ──────────────────────────────────────────────────────────────
def cmd_balance(a):
    ledger = collateral.CollateralLedger()
    if a.hotkey:
        rao = ledger.balance_of(a.hotkey)
        print(f"{a.hotkey}: {rao} rao ({rao / collateral.RAO_PER_ALPHA:.4f} alpha)")
    else:
        print(f"total:   {ledger.total() / collateral.RAO_PER_ALPHA:.4f} alpha")
        print(f"slashed: {ledger.slashed() / collateral.RAO_PER_ALPHA:.4f} alpha")


def cmd_deposit(a):
    hex_data = a.extrinsic_hex or open(a.file).read().strip()
    print(json.dumps(process_deposit(hex_data), indent=2))


def cmd_withdraw(a):
    print(json.dumps(process_withdraw(json.loads(open(a.request).read())), indent=2))


def cmd_slash_eliminated(a):
    ledger = collateral.CollateralLedger()
    db = sqlite3.connect(config.DB_PATH)
    rows = db.execute(
        "SELECT hotkey, eliminated_t0 FROM hotkey_meta "
        "WHERE eliminated_t0 IS NOT NULL AND slash_tx IS NULL").fetchall()
    if not rows:
        print("no pending eliminations")
        return
    st = _st() if a.execute else None
    for hotkey, t0 in rows:
        balance = ledger.balance_of(hotkey)
        amount = int(balance * config.ELIM_SLASH_PROPORTION)
        print(f"{hotkey} eliminated at t0={t0:.0f} · collateral {balance} rao "
              f"· slash {amount} rao{'' if a.execute else ' (dry-run)'}")
        if not a.execute or amount == 0:
            continue
        addr, key = _owner_evm()
        tx = collateral.ledger_write("slash", hotkey, amount, addr, key)
        burn_ext = collateral.create_burn_alpha_extrinsic(
            st, _vault_wallet(), hotkey, amount)
        receipt = st.substrate.submit_extrinsic(burn_ext, wait_for_inclusion=True)
        if not receipt.is_success:
            print(f"  ! burn_alpha failed AFTER ledger slash {tx} — "
                  f"funds remain in slashedCollateral pool, reconcile manually: "
                  f"{receipt.error_message}")
        db.execute("UPDATE hotkey_meta SET slash_rao=?, slash_tx=? WHERE hotkey=?",
                   (amount, tx, hotkey))
        db.commit()
        print(f"  burned · ledger tx {tx}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the self-serve collateral HTTP API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8189)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("balance")
    s.add_argument("--hotkey", default="")
    s.set_defaults(fn=cmd_balance)

    s = sub.add_parser("deposit")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--extrinsic-hex")
    g.add_argument("--file")
    s.set_defaults(fn=cmd_deposit)

    s = sub.add_parser("withdraw")
    s.add_argument("--request", required=True, help="signed request JSON file")
    s.set_defaults(fn=cmd_withdraw)

    s = sub.add_parser("slash-eliminated")
    s.add_argument("--execute", action="store_true",
                   help="actually slash+burn (default: dry-run)")
    s.set_defaults(fn=cmd_slash_eliminated)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
