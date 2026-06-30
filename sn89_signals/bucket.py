"""Blob transport — R2/S3 upload (miner) and HTTPS fetch (validator/owner).

The transport is untrusted: integrity comes from the on-chain commitment hash
and the AEAD bindings, not from the bucket. A miner may serve blobs from any
public HTTPS URL; the reference layout is {base}/{hotkey}/{nonce}.json.
"""
from __future__ import annotations

import json
import time

import requests

from . import config


import os

# Local-disk transport for single-host testnet soaks: set SN89_BLOB_DIR and
# serve it with any static HTTP server at SN89_R2_PUBLIC_BASE. Upload = write
# file. The URL/url_tag/fetch logic is identical to the R2 path.
BLOB_DIR = os.getenv("SN89_BLOB_DIR", "")


def blob_url(hotkey: str, nonce: str, base: str | None = None) -> str:
    base = (base or config.R2_PUBLIC_BASE).rstrip("/")
    return f"{base}/{hotkey}/{nonce}.json"


def _relay_upload(blob: dict, hotkey: str, nonce: str) -> str:
    """POST the blob to the owner-hosted relay; returns the public URL it
    serves. Transport-only — the blob is already encrypted and the relay
    can't read or forge it (integrity is the on-chain commitment)."""
    r = requests.post(
        config.RELAY_URL,
        headers={"Authorization": f"Bearer {config.RELAY_TOKEN}"},
        json={"hotkey": hotkey, "nonce": nonce, "blob": blob},
        timeout=10,
    )
    if r.status_code != 200:
        raise RuntimeError(f"relay upload failed: HTTP {r.status_code} {r.text[:200]}")
    url = r.json().get("url")
    if not url:
        raise RuntimeError("relay upload: no url in response")
    return url


def mirror_to_relay(blob: dict, hotkey: str, nonce: str) -> bool:
    """Best-effort: deliver the encrypted blob to the owner-hosted relay for
    real-time owner visibility, WITHOUT changing the canonical host.

    Self-hosting miners (own bucket / local disk) call this so the owner sees a
    submission the instant it lands — the relay is the owner's realtime intake —
    while the miner's own URL stays canonical in the on-chain commitment. The
    blob is already encrypted; the relay can't read or forge it. No-op (returns
    False) without a relay/feed token, and never raises: owner delivery must not
    block a submission."""
    if not config.RELAY_TOKEN:
        return False
    try:
        _relay_upload(blob, hotkey, nonce)
        return True
    except Exception:  # noqa: BLE001
        return False


def _s3_client():
    """R2/S3 client, or a clear error naming the missing config.

    Only reached when neither SN89_BLOB_DIR nor the relay path applies, so an
    incomplete (or absent) R2 config here would otherwise surface as a cryptic
    botocore `ValueError: Invalid endpoint:` — name the actual fix instead.
    """
    missing = [name for name, val in (
        ("SN89_R2_ENDPOINT", config.R2_ENDPOINT),
        ("SN89_R2_BUCKET", config.R2_BUCKET),
        ("SN89_R2_ACCESS_KEY_ID", config.R2_ACCESS_KEY_ID),
        ("SN89_R2_SECRET_ACCESS_KEY", config.R2_SECRET_ACCESS_KEY),
    ) if not val]
    if missing:
        raise RuntimeError(
            "no blob transport configured. Set SN89_RELAY_TOKEN (or "
            "SN89_FEED_TOKEN) to publish through the owner relay [zero setup], "
            "or SN89_BLOB_DIR to host locally, or provide the full R2 config "
            f"(missing: {', '.join(missing)}).")
    import boto3  # lazy — validators never need it
    return boto3.client(
        "s3",
        endpoint_url=config.R2_ENDPOINT,
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
    )


def upload(blob: dict, hotkey: str, nonce: str) -> str:
    """Upload a blob; returns its public URL.

    Transport precedence: local disk (SN89_BLOB_DIR, testnet) → your own S3/R2
    (SN89_R2_* creds) → owner-hosted relay (SN89_RELAY_TOKEN / feed token). The
    relay is the zero-setup fallback for traders without a bucket.
    """
    if BLOB_DIR:
        d = os.path.join(BLOB_DIR, hotkey)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{nonce}.json"), "w", encoding="utf-8") as fh:
            json.dump(blob, fh, separators=(",", ":"))
        return blob_url(hotkey, nonce)
    if not config.R2_ACCESS_KEY_ID and config.RELAY_TOKEN:
        return _relay_upload(blob, hotkey, nonce)
    s3 = _s3_client()
    key = f"{hotkey}/{nonce}.json"
    s3.put_object(
        Bucket=config.R2_BUCKET,
        Key=key,
        Body=json.dumps(blob, separators=(",", ":")).encode(),
        ContentType="application/json",
    )
    return blob_url(hotkey, nonce)


def update_index(hotkey: str, nonce: str, keep: int = 200) -> None:
    """Append nonce to {hotkey}/index.json (validators/owner discover blobs
    through this listing, then verify via the on-chain url_tag — the index is
    untrusted convenience, not integrity)."""
    # Precedence MUST mirror upload(): local disk first. A hosted follower
    # always sets SN89_FEED_TOKEN (=> RELAY_TOKEN truthy) yet also has a
    # BLOB_DIR, so upload() writes the blob to LOCAL disk while the relay
    # never receives it. Checking RELAY_TOKEN first here would early-return
    # and leave that local blob unindexed => validators can never discover
    # it. Index locally whenever the blob was hosted locally.
    if BLOB_DIR:
        d = os.path.join(BLOB_DIR, hotkey)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "index.json")
        try:
            with open(p, encoding="utf-8") as fh:
                cur = json.load(fh)
        except Exception:
            cur = {"nonces": []}
        nonces = [n for n in cur.get("nonces", []) if n != nonce] + [nonce]
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"nonces": nonces[-keep:]}, fh)
        return
    if not config.R2_ACCESS_KEY_ID and config.RELAY_TOKEN:
        return  # relay maintains the index server-side on each blob POST
    s3 = _s3_client()
    key = f"{hotkey}/index.json"
    try:
        cur = json.loads(s3.get_object(Bucket=config.R2_BUCKET, Key=key)["Body"].read())
    except Exception:
        cur = {"nonces": []}
    nonces = [n for n in cur.get("nonces", []) if n != nonce] + [nonce]
    s3.put_object(Bucket=config.R2_BUCKET, Key=key,
                  Body=json.dumps({"nonces": nonces[-keep:]}).encode(),
                  ContentType="application/json")


def fetch(url: str, timeout: int = 10, max_bytes: int = 64 * 1024,
          deadline_s: float | None = None) -> dict | None:
    """Fetch a blob; size-capped AND total-time-capped. Returns None on any
    failure.

    The blob host is UNTRUSTED. `requests`' `timeout` only bounds the gap
    BETWEEN bytes, so a malicious host can drip one byte just under it forever
    and stall the caller for days (the validator runs this on its synchronous
    loop — a stall stops it ingesting, grading, and setting weights). So we also
    enforce a hard wall-clock `deadline_s` across the whole read, streaming
    byte-granularly so the deadline is honoured no matter how slowly bytes
    arrive, and cap total size. Whatever exceeds either bound is abandoned
    (returns None); the caller retries from the durable on-chain commitment.
    """
    if deadline_s is None:
        deadline_s = config.BLOB_FETCH_DEADLINE_S
    r = None
    try:
        start = time.monotonic()
        r = requests.get(url, timeout=timeout, stream=True)
        if r.status_code != 200:
            return None
        buf = bytearray()
        # chunk_size=1 so the deadline is checked at byte granularity — a larger
        # chunk would let iter_content block (filling the chunk) past the
        # deadline under a slow drip. Blobs are tiny (≤ max_bytes), so the
        # iteration count is bounded and cheap.
        for chunk in r.iter_content(chunk_size=1):
            if time.monotonic() - start > deadline_s:
                return None                     # slow-drip / stall — bail
            buf += chunk
            if len(buf) > max_bytes:
                return None                     # oversize
        return json.loads(bytes(buf))
    except Exception:
        return None
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass
