"""Blob transport — R2/S3 upload (miner) and HTTPS fetch (validator/owner).

The transport is untrusted: integrity comes from the on-chain commitment hash
and the AEAD bindings, not from the bucket. A miner may serve blobs from any
public HTTPS URL; the reference layout is {base}/{hotkey}/{nonce}.json.
"""
from __future__ import annotations

import json

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


def upload(blob: dict, hotkey: str, nonce: str) -> str:
    """Upload a blob; returns its public URL.

    Local mode (SN89_BLOB_DIR set): writes to disk for a co-located static
    server. Otherwise S3/R2 via SN89_R2_* env.
    """
    if BLOB_DIR:
        d = os.path.join(BLOB_DIR, hotkey)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{nonce}.json"), "w", encoding="utf-8") as fh:
            json.dump(blob, fh, separators=(",", ":"))
        return blob_url(hotkey, nonce)
    import boto3  # lazy — validators never need it

    s3 = boto3.client(
        "s3",
        endpoint_url=config.R2_ENDPOINT,
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
    )
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
    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url=config.R2_ENDPOINT,
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
    )
    key = f"{hotkey}/index.json"
    try:
        cur = json.loads(s3.get_object(Bucket=config.R2_BUCKET, Key=key)["Body"].read())
    except Exception:
        cur = {"nonces": []}
    nonces = [n for n in cur.get("nonces", []) if n != nonce] + [nonce]
    s3.put_object(Bucket=config.R2_BUCKET, Key=key,
                  Body=json.dumps({"nonces": nonces[-keep:]}).encode(),
                  ContentType="application/json")


def fetch(url: str, timeout: int = 10, max_bytes: int = 64 * 1024) -> dict | None:
    """Fetch a blob; size-capped, returns None on any failure."""
    try:
        r = requests.get(url, timeout=timeout, stream=True)
        if r.status_code != 200:
            return None
        raw = r.raw.read(max_bytes + 1, decode_content=True)
        if len(raw) > max_bytes:
            return None
        return json.loads(raw)
    except Exception:
        return None
