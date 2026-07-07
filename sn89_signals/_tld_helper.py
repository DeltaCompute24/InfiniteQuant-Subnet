#!/usr/bin/env python3
"""Run timelock.tld() in a disposable process: a malformed (or wrong-version)
ciphertext panics the Rust extension (SIGABRT), which must never take the
validator down.

Usage: python _tld_helper.py <ct_hex> <sig_hex> [<pubkey_hex>]
  - prints the tld result hex on stdout.
  - if <pubkey_hex> is given, uses it directly (so this file can run in a
    minimal sidecar venv that has ONLY `timelock` installed — no sn89_signals
    import). Without it, falls back to sn89_signals.config.DRAND_PUBLIC_KEY.
"""
import sys

from timelock import Timelock

if __name__ == "__main__":
    ct = bytes.fromhex(sys.argv[1])
    sig = bytes.fromhex(sys.argv[2])
    if len(sys.argv) > 3 and sys.argv[3]:
        pubkey = sys.argv[3]
    else:
        from sn89_signals import config
        pubkey = config.DRAND_PUBLIC_KEY
    raw = Timelock(pubkey).tld(ct, sig)
    print(raw if isinstance(raw, str) else bytes(raw).hex())
