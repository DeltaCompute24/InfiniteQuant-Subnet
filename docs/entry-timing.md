# Entry-timing precision — design

**Status:** proposed · 2026-06-06
**Problem:** "block time + 30 s" entry anchoring is imprecise — but the imprecision is NOT
where the README implies. This doc separates the three error sources, sizes them, and
specifies the fix in two phases. Phase 1 needs no protocol change and removes ~95% of the
error. Phase 2 (Vanta-style ms receipt stamps) is specced but deliberately deferred.

---

## 1. Where the imprecision actually is

| # | Source | Size | Validator-divergent? | Gameable? |
|---|--------|------|----------------------|-----------|
| A | `first_seen_block ≈ T0` via poll loop (`neurons/validator.py:83`) | 0 – poll interval (≤30 s), **different per validator** | **YES** | no |
| B | Entry price = open of first **1-minute** bar at/after anchor (`sn89_signals/polygon.py:73`) | 0 – 60 s quantization | no (deterministic) | no |
| C | ~12 s block cadence — miner can't pick the exact block | ±1 block | no | no (anti-cherry-pick feature) |
| D | Substrate block timestamp resolution | **none** — Timestamp pallet is ms-precise (`chain.py:84`) | no | no |

A is the bug: each validator journals T0 at whatever block its own `ingest()` poll
happened to run, so validators disagree about a signal's T0 by up to a poll cycle, and a
slow validator systematically grades a *different entry price* than a fast one.
B wastes whatever precision T0 has: a ms-exact anchor snapped to the next minute open.
C is irreducible while the on-chain commitment is the entry event — and it is the
property that makes the protocol un-backdatable. Do not try to remove it.

## 2. Phase 1 — deterministic T0 + second-level entry (no protocol change)

### 2.1 T0 = the commitment's true inclusion block (kills A)

Substrate stores `Commitments.CommitmentOf(netuid, hotkey)` as
`Registration { block, deposit, info }` — the **`block` field is the inclusion block of
the last `set_commitment`**. Every validator reads the identical value regardless of poll
phase. No journaling race, no first-seen heuristics.

`bittensor`'s `get_all_commitments()` strips the `block` field, so query storage raw:

```python
# sn89_signals/chain.py
def read_all_commitments_with_block(self) -> dict[str, dict]:
    """{hotkey: {**decoded, 'commit_block': int}} — commit_block is consensus-exact."""
    out = {}
    qm = self.st.substrate.query_map("Commitments", "CommitmentOf", [self.netuid])
    for hotkey, reg in qm:
        v = reg.value
        dec = decode_commitment(extract_info_str(v["info"]))   # same fields decode as today
        if dec:
            dec["hotkey"] = hotkey.value
            dec["commit_block"] = v["block"]                   # ← the real T0 block
            out[hotkey.value] = dec
    return out
```

Validator change (`neurons/validator.py` `ingest()`):

```python
t0 = self.ch.block_time_unix(c["commit_block"])   # ms-precise, identical on every validator
... INSERT ... first_seen_block → commit_block ...
```

Caveat: subtensor keeps ONE commitment per hotkey (latest wins). If a miner overwrites
before a validator's poll observes the previous commitment, the old signal is lost.
With no minimum spacing a miner *can* fire back-to-back, so a fresh commit may land
within a poll interval of the prior one; a ≤30 s poll keeps that window small. Keep the
journal as an *observation log*; `commit_block` is canon.

`t0_unix` becomes **float seconds with ms precision** (it already is — `chain.py:84`
divides the pallet's ms value). Store `t0_ms` as INTEGER ms instead to stop precision
loss in SQLite/JSON round-trips.

### 2.2 Entry price from 1-second aggregates (kills B)

Replace the minute-open anchor in `sn89_signals/polygon.py:entry_price_at` with
second-level aggs; fall back to the minute bar only when the second feed has a gap:

```python
def entry_price_at(asset, asset_class, t0_ms):
    anchor_ms = t0_ms + config.LATENCY_BUFFER_S * 1000
    # 1-second aggs: /v2/aggs/ticker/{t}/range/1/second/{from}/{to}
    bars = second_aggs(asset, asset_class, anchor_ms, anchor_ms + 120_000)
    for b in bars:
        if b["t"] >= anchor_ms:
            return b["o"]                      # open of first 1-s bar at/after anchor
    return _minute_fallback(asset, asset_class, anchor_ms)   # current behavior
```

Determinism is preserved: every validator asking Polygon for the same window gets the
same bar (same property the minute version relies on). Coverage notes:
crypto `X:` has dense 1-s aggs; FX `C:` is quote-driven — 1-s aggs exist but are sparse
off-hours, hence the 120 s scan + minute fallback; same for metals CFDs.
Grading (TP/SL touch) **stays on 1-minute candles** — only the entry anchor sharpens.

### 2.3 Residual error after Phase 1

Only C remains: the miner's signal lands in a block they don't fully control, ±1 block
(~12 s) of un-gameable, symmetric timing noise, then an exact ms anchor at
`block_ts_ms + 30_000` priced off a 1-second bar. Against ±150–300 bps TP/SL bands and
minute-candle grading, 12 s of symmetric entry noise is slippage, not signal. All
validators now grade the **same entry price** — removing the silent cross-validator
weight disagreement A causes today.

## 3. Phase 2 — Vanta-style ms receipt stamps (specced, DEFERRED)

What the Vanta/PTN audit (2026-06-06, `~/projects/sn8-testnet/proprietary-trading-network`)
actually found:

- The ms stamp is just per-validator wall clock at axon receipt
  (`neurons/validator.py:602`, `now_ms = TimeUtil.now_in_millis()`).
- Cross-validator reconciliation tolerates **3 minutes** of stamp divergence
  (`validator_sync_base.py:526`, `SYNC_LOOK_AROUND_MS`).
- The decentralized P2P consensus (`p2p_syncer.py`) is deprecated shadow code; production
  snaps every validator to a **centralized golden checkpoint from Taoshi's GCS bucket**
  (`auto_sync.py:65`). Vanta never made decentralized ms consensus work.

If SN89 ever needs sub-block entry timing, the bounded version is:

1. Miner pushes the **ciphertext** to every validator in real time (REST or axon) at the
   same moment it uploads to the bucket; each validator stamps `recv_ms` locally.
2. `set_commitment` stays the audit anchor. Each validator enforces
   `block_ts(commit_block - 1) ≤ recv_ms ≤ block_ts(commit_block) + GRACE` — a rogue or
   skewed clock can shade entry by at most one block window, never backdate past it.
   (This bound is the thing Vanta *doesn't* have.)
3. **No stamp consensus.** Each validator grades from its own `recv_ms`; sub-second
   divergence flips a touch-first outcome only in vanishing edge cases, and Yuma weight
   consensus absorbs it — the same reason Vanta survives a 3-minute tolerance.
4. Cost: miners need live connectivity to all validators; validators become always-on
   ingest servers; a new liveness failure mode (push received, commit missing → ignore;
   commit seen, push missing → fall back to Phase 1 block anchor).

Defer because Phase 1's residual error (C) is symmetric and un-gameable, grading is
minute-granular, and Phase 2 buys ≤12 s of anchor sharpness for a meaningfully larger
attack/ops surface. Revisit only if horizons shrink to intraday-minutes or grading moves
to second candles.

## 4. Migration

- `signals` table: add `commit_block INTEGER`, `t0_ms INTEGER`; backfill from
  `CommitmentOf` queried at historical block hashes (storage is archival), then derive
  `t0_ms = Timestamp.Now @ commit_block`. Keep `first_seen_block` as observational.
- Re-grade nothing retroactively — flag the cutover block in `hotkey_meta` and apply the
  new anchor to signals committed after it (consensus rule change; version-gate it in
  `config.py` like the §6.4 constants).
- README: replace "validators derive the entry price from market data at that block's
  time + 30 s" granularity claim with: *"entry = open of the first 1-second aggregate at
  or after commit-block timestamp + 30 s; the commit block is read from on-chain
  storage, so every validator derives the identical entry price."*
