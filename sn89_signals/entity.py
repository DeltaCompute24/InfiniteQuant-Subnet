"""Signals entity accounts: one UID scoring many subaccounts (HF only, for now).

An account id is `<entity_hk>_<n>` (the same shape markets.py uses). Every record that
was keyed by hotkey is keyed by the SCORING KEY instead: the account for a valid entity
call, the signer's hotkey for everything else. Only the step that maps keys to UIDs knows
an account belongs to its entity, so gates, rate caps and grading need no other change.

See config.SIGNALS_ENTITY_* for the arm and the allow-list, and why both exist.
"""
from __future__ import annotations

import re

from . import config

ACCOUNT_RE = re.compile(r"^(5[1-9A-HJ-NP-Za-km-z]{46,47})_(\d{1,9})$")


class EntityRejected(ValueError):
    """A submission's account claim is not valid. The message is the reject reason."""


def parse(account) -> tuple[str, int] | None:
    m = ACCOUNT_RE.match(str(account or ""))
    return (m.group(1), int(m.group(2))) if m else None


def is_agent_account(key) -> bool:
    """An Agent-competition account: an entity account numbered at or above
    config.AGENT_ACCOUNT_MIN. Scores in `agent`, never in `lf`."""
    parsed = parse(key)
    return (parsed is not None and parsed[0] in config.SIGNALS_ENTITY_HOTKEYS
            and parsed[1] >= config.AGENT_ACCOUNT_MIN)


def check_claim(signer: str, payload: dict | None, t_unix: float) -> None:
    """Ingest-side validation. No `account` field is always fine (a plain call)."""
    acct = (payload or {}).get("account")
    if acct is None:
        return
    if not config.signals_entity_active(t_unix):
        raise EntityRejected("entity_not_armed")
    parsed = parse(acct)
    if parsed is None:
        raise EntityRejected("bad_account")
    owner, _n = parsed
    if owner != signer:
        raise EntityRejected("account_not_signer")
    if signer not in config.SIGNALS_ENTITY_HOTKEYS:
        raise EntityRejected("not_an_entity")


def scoring_key(signer: str, payload: dict | None, t_unix: float | None) -> str:
    """The key a call is scored under. Any claim that would fail check_claim scores
    under the signer, so a grader never credits an account ingest should have refused."""
    if t_unix is None:
        return signer
    try:
        check_claim(signer, payload, float(t_unix))
    except EntityRejected:
        return signer
    acct = (payload or {}).get("account")
    return str(acct) if acct is not None else signer


def with_accounts(uid_by_hk: dict, keys) -> dict:
    """uid_by_hk plus every account in `keys` whose entity hotkey holds a UID.
    An account whose entity is not registered resolves to nothing, like an
    unregistered hotkey."""
    out = dict(uid_by_hk)
    for k in keys:
        if k in out:
            continue
        parsed = parse(k)
        if parsed is None or parsed[0] not in config.SIGNALS_ENTITY_HOTKEYS:
            continue
        uid = uid_by_hk.get(parsed[0])
        if uid is not None:
            out[k] = uid
    return out


# ── LF batches ───────────────────────────────────────────────────────────────
BATCH_KIND = "sn89.batch"
BATCH_MAX_ITEMS = 512
BATCH_MAX_BYTES = 1024 * 1024        # fetch cap for a batch blob (~1.8 KB per item)
# The owner relay (partner-webhook /api/sn89/blob) refuses anything over 64 KiB: ~30 calls.
BATCH_RELAY_MAX_BYTES = 63 * 1024
BATCH_RELAY_MAX_ITEMS = 30


def batch_leaf(pt: bytes) -> bytes:
    """A batch leaf is the call's own LF commitment: sha256 of its canonical bytes."""
    import hashlib
    return hashlib.sha256(pt).digest()


def batch_root(pts: list[bytes]) -> str:
    from . import hf
    return hf.merkle_root([batch_leaf(p) for p in pts])


def expand_batch(blob: dict, root_hex: str, entity_hk: str, t0_unix: float,
                 decrypt) -> list[tuple[str, bytes, dict]]:
    """Open an entity batch. Returns [(commit_hex, plaintext, item_blob)], one per
    account call, in blob order. Raises EntityRejected(reason) if ANY item fails:
    a batch is all or nothing, so an entity cannot drop single calls from it.

    `decrypt(item_blob) -> bytes | None` is the validator's timelock decrypt. Each
    plaintext must be a valid Signal whose `hotkey` is an account of this entity."""
    from .schema import Signal, ValidationError, validate
    if not isinstance(blob, dict) or blob.get("kind") != BATCH_KIND:
        raise EntityRejected("batch_bad_blob")
    items = blob.get("items")
    if not isinstance(items, list) or not items or len(items) > BATCH_MAX_ITEMS:
        raise EntityRejected("batch_bad_items")
    if str(blob.get("owner_pk", "")).lower() != config.OWNER_PK_HEX.lower() or any(
            str((it or {}).get("owner_pk", "")).lower() != config.OWNER_PK_HEX.lower()
            for it in items):
        raise EntityRejected("wrong_owner_pk")
    pts = []
    for it in items:
        pt = decrypt(it)
        if pt is None:
            raise EntityRejected("batch_decrypt_failed")
        pts.append(pt)
    if batch_root(pts) != root_hex:
        raise EntityRejected("batch_root_mismatch")
    out, seen = [], set()
    for pt, it in zip(pts, items):
        try:
            sig = validate(Signal.from_bytes(pt), t0_unix=t0_unix)
            check_claim(entity_hk, {"account": sig.hotkey}, t0_unix)
        except (ValidationError, EntityRejected, Exception) as e:  # noqa: BLE001
            raise EntityRejected(f"batch_invalid_item:{e}")
        commit_hex = batch_leaf(pt).hex()
        if commit_hex in seen:
            raise EntityRejected("batch_duplicate_item")
        seen.add(commit_hex)
        out.append((commit_hex, pt, it))
    return out


# ── one commitment slot, two writers ─────────────────────────────────────────
# The Markets entity hotkey commits oracle anchors (neurons/markets_oracle_anchor.py)
# AND, as the signals entity, LF batches and account claims. Both writers take this
# lock around every set_commitment: one signer means one nonce sequence, and
# CommitmentOf is latest-wins, so two commits less than a validator poll apart
# (config.POLL_INTERVAL_S) can hide the first from a validator that does not scan
# history. COMMIT_MIN_GAP_S is two polls.
COMMIT_LOCK = "/run/lock/sn89-entity-commit.lock"
COMMIT_MIN_GAP_S = 2 * config.POLL_INTERVAL_S


def _last_commit(path: str) -> float:
    """Wall-clock time of the last commit by any writer, written INTO the `.last` file.
    Not its mtime: Linux stamps mtime from a coarse clock that can trail time.time() by a
    few ms, which made the enforced gap come up short (caught by the iq-main test run)."""
    try:
        with open(path + ".last") as f:
            return float(f.read().strip() or 0.0)
    except (OSError, ValueError):
        return 0.0


def slot_ready(path: str = COMMIT_LOCK, min_gap_s: float = COMMIT_MIN_GAP_S) -> bool:
    """Non-blocking check for a caller that must not stall (the multiplexer's loop also
    carries HF latency): True when commit_slot() would not have to wait for the gap."""
    import time
    return time.time() - _last_commit(path) >= min_gap_s


class commit_slot:
    """`with commit_slot(): set_commitment(...)` — blocks until the slot is free and
    at least COMMIT_MIN_GAP_S has passed since the last commit by any writer."""

    def __init__(self, path: str = COMMIT_LOCK, min_gap_s: float = COMMIT_MIN_GAP_S):
        self.path, self.min_gap_s = path, min_gap_s

    def __enter__(self):
        import fcntl
        import os
        import time
        self.fh = open(self.path, "a+")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        while True:                                   # sleep can return early; re-check
            wait = _last_commit(self.path) + self.min_gap_s - time.time()
            if wait <= 0:
                break
            time.sleep(wait)
        return self

    def __exit__(self, *exc):
        import fcntl
        import time
        try:
            with open(self.path + ".last", "w") as f:
                f.write(repr(time.time()))
        finally:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
        return False
