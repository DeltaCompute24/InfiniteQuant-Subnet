#!/usr/bin/env python3
"""Run timelock.tld() in a disposable process: a malformed ciphertext panics the
Rust extension (SIGABRT), which must never take the validator down.
Usage: python _tld_helper.py <ct_hex> <sig_hex>  -> prints result hex on stdout."""
import sys

from timelock import Timelock

from sn89_signals import config

if __name__ == "__main__":
    ct = bytes.fromhex(sys.argv[1])
    sig = bytes.fromhex(sys.argv[2])
    raw = Timelock(config.DRAND_PUBLIC_KEY).tld(ct, sig)
    print(raw if isinstance(raw, str) else bytes(raw).hex())
