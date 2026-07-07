"""Dual-envelope encryption — adapted from MANTIS (SN123) v2 payloads, MIT.

One content ciphertext, with two key-wraps on every submission:

    pt ──ChaCha20Poly1305(key, AAD=binding)──► C
    key wrapped to owner:    W_owner = ChaCha(HKDF(X25519(ske, owner_pk)), key)
    (ske‖key) timelocked:    W_time  = tlock(round, ske‖key)

* `W_owner` is a key-wrap to the subnet-owner X25519 key — a protocol
  requirement on every blob; a submission without a valid owner wrap is not
  gradeable.
* VALIDATORS/public decrypt after `round` matures: drand sig → tld → ske‖key
  → verify pke = pub(ske) → open C.
* `binding = SHA256(hotkey:round:owner_pk:pke)` is the AAD on every AEAD —
  a blob can't be replayed under another hotkey or round.

The plaintext is the Signal's canonical bytes; SHA256(pt) must equal the
on-chain commitment (checked before anything is trusted).
"""
from __future__ import annotations

import hashlib
import os
import secrets
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from timelock import Timelock

from . import config

import os
import subprocess
import sys

_TLD_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tld_helper.py")


# Legacy timelock sidecar: the mainnet validator runs timelock 0.0.2.dev0
# (timelock_wasm_wrapper 0.3.0, 356-byte W_time). Self-hosted miners who install
# the repo-pinned timelock==0.0.1.dev0 (wasm_wrapper 0.0.2) seal a 372-byte
# W_time whose format the 0.0.2 tld() cannot open (it Rust-aborts). This points a
# python from a venv that has ONLY timelock 0.0.1.dev0 installed, so we can open
# BOTH formats and never wrongly void/strike an honest miner over a version skew.
_TLD_FALLBACK_PY = os.getenv(
    "SN89_TLD_FALLBACK_PYTHON", "/opt/sn89-signals/.venv-tl001/bin/python")


def _run_tld_helper(python_exe: str, ct: bytes, drand_sig: bytes,
                    timeout: float, pass_pubkey: bool):
    argv = [python_exe, _TLD_HELPER, ct.hex(), drand_sig.hex()]
    if pass_pubkey:
        argv.append(config.DRAND_PUBLIC_KEY)  # sidecar venv lacks sn89_signals
    try:
        r = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            cwd=os.path.dirname(_TLD_HELPER),
            env={**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(_TLD_HELPER))},
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    if r.returncode != 0:
        return None, f"rc={r.returncode}"
    try:
        return bytes.fromhex(r.stdout.strip()), None
    except ValueError:
        return None, "bad-output"


def _tld_isolated(ct: bytes, drand_sig: bytes, timeout: float = 30.0):
    """timelock.tld() in a throwaway subprocess -- a hostile/malformed (or simply
    wrong-timelock-version) ciphertext can abort the Rust extension; isolation
    turns that into None (voidable) instead of killing the validator.

    Two-version opener: try the validator's own timelock (0.0.2) first, then the
    legacy 0.0.1 sidecar. A blob that opens under EITHER version is honest and is
    graded normally -- only a blob that fails BOTH is voided."""
    # primary: validator's own timelock (currently 0.0.2.dev0)
    res, _ = _run_tld_helper(sys.executable, ct, drand_sig, timeout, pass_pubkey=False)
    if res is not None:
        return res
    # fallback: legacy timelock 0.0.1.dev0 (self-hosters on the repo-pinned pin)
    if _TLD_FALLBACK_PY and os.path.exists(_TLD_FALLBACK_PY):
        res, err = _run_tld_helper(_TLD_FALLBACK_PY, ct, drand_sig, timeout, pass_pubkey=True)
        if res is not None:
            print("  ↺ opened via legacy timelock 0.0.1 fallback")
            return res
    print("  ⚠ tld unopenable under any known timelock version -- voidable blob")
    return None


_WRAP_INFO = b"sn89-owner-wrap"


# ── helpers ──────────────────────────────────────────────────────────────────
def _hkdf_key(shared: bytes, key_len: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=key_len, salt=None, info=_WRAP_INFO).derive(shared)


def _binding(hotkey: str, rnd: int, owner_pk: bytes, pke: bytes) -> bytes:
    h = hashes.Hash(hashes.SHA256())
    for part in (hotkey.encode(), b":", str(rnd).encode(), b":", owner_pk, b":", pke):
        h.update(part)
    return h.finalize()


def _derive_pke(ske_raw: bytes) -> bytes:
    return X25519PrivateKey.from_private_bytes(ske_raw).public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)


def target_round(reveal_at_unix: float) -> int:
    """drand quicknet round that matures at (or just after) `reveal_at_unix`."""
    return int((reveal_at_unix - config.DRAND_GENESIS_TIME) // config.DRAND_PERIOD_S) + 1


def round_time(rnd: int) -> float:
    return config.DRAND_GENESIS_TIME + rnd * config.DRAND_PERIOD_S


def fetch_drand_signature(rnd: int, timeout: int = 10) -> bytes | None:
    """None until the round matures."""
    url = f"{config.DRAND_API}/beacons/{config.DRAND_BEACON_ID}/rounds/{rnd}"
    r = requests.get(url, timeout=timeout)
    if r.status_code != 200:
        return None
    return bytes.fromhex(r.json()["signature"])


# ── encrypt (miner) ──────────────────────────────────────────────────────────
def encrypt(pt: bytes, hotkey: str, rnd: int, owner_pk_hex: str) -> dict:
    owner_pk = bytes.fromhex(owner_pk_hex)
    ske = os.urandom(32)
    key = os.urandom(32)
    pke = _derive_pke(ske)
    binding = _binding(hotkey, rnd, owner_pk, pke)

    c_nonce = os.urandom(12)
    c_ct = ChaCha20Poly1305(key).encrypt(c_nonce, pt, binding)

    shared = X25519PrivateKey.from_private_bytes(ske).exchange(X25519PublicKey.from_public_bytes(owner_pk))
    wrap_nonce = os.urandom(12)
    w_owner_ct = ChaCha20Poly1305(_hkdf_key(shared)).encrypt(wrap_nonce, key, binding)

    tlock = Timelock(config.DRAND_PUBLIC_KEY)
    w_time_ct = tlock.tle(rnd, (ske + key).hex(), secrets.token_bytes(32))

    return {
        "v": 1,
        "round": rnd,
        "hk": hotkey,
        "owner_pk": owner_pk_hex,
        "commit": hashlib.sha256(pt).hexdigest(),
        "C": {"nonce": c_nonce.hex(), "ct": c_ct.hex()},
        "W_owner": {"pke": pke.hex(), "nonce": wrap_nonce.hex(), "ct": w_owner_ct.hex()},
        "W_time": {"ct": bytes(w_time_ct).hex()},
        "binding": binding.hex(),
        "alg": config.ALG_LABEL,
    }


# ── decrypt: timelock path (validators / public, post-reveal) ────────────────
def decrypt_timelock(blob: dict, drand_sig: bytes, tlock: Timelock | None = None) -> bytes | None:
    try:
        tlock = tlock or Timelock(config.DRAND_PUBLIC_KEY)
        owner_pk = bytes.fromhex(blob["owner_pk"])
        pke = bytes.fromhex(blob["W_owner"]["pke"])
        binding = _binding(blob["hk"], int(blob["round"]), owner_pk, pke)
        if binding != bytes.fromhex(blob["binding"]):
            return None
        raw = _tld_isolated(bytes.fromhex(blob["W_time"]["ct"]), drand_sig)
        if raw is None:
            return None
        skek = bytes(raw)
        if len(skek) == 128:  # hex-encoded bytes round-trip (MANTIS quirk)
            try:
                skek = bytes.fromhex(skek.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                pass
        if len(skek) != 64:
            return None
        ske, key = skek[:32], skek[32:]
        if _derive_pke(ske) != pke:
            return None
        pt = ChaCha20Poly1305(key).decrypt(
            bytes.fromhex(blob["C"]["nonce"]), bytes.fromhex(blob["C"]["ct"]), binding)
        if hashlib.sha256(pt).hexdigest() != blob.get("commit"):
            return None
        return pt
    except Exception:
        return None


def expected_round_ok(blob_round: int, commit_unix: float) -> bool:
    """§5: blob round must sit within ±ROUND_TOLERANCE_S of commit+REVEAL_DELAY."""
    want = commit_unix + config.REVEAL_DELAY_S
    return abs(round_time(int(blob_round)) - want) <= config.ROUND_TOLERANCE_S


