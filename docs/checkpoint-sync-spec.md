# SN89 Validator Checkpoint + Auto-Sync (trust-minimized, on-chain-anchored)

**Problem.** SN89's scoring is a pure function of the per-hotkey decisive journal, so
two validators holding the same journal compute identical weights. A validator that
joins *days after* others cannot rebuild that journal today: the on-chain layer only
exposes each hotkey's **latest** commitment (Bittensor `CommitmentOf` overwrites), the
history scan starts at `block-1` not genesis, and the signal **content** lives in an
off-chain blob whose relay index only exposes the last ~50 nonces and which forfeits to
`LOST` if not captured by `reveal+grace`. Result: a late validator gets a recent, harsher
subset → divergent weights → VTRUST loss → emission drift.

**Precedent (SN8 / Vanta).** Orders are off-chain; a trusted **mothership** validator
publishes a full `validator_checkpoint.json.gz`, and followers `restore`/`auto_sync` from
it (`compute_delta_with_mothership.py`). Catch-up works, but it is **blind trust** — if the
mothership forges or omits, followers have no recourse and no on-chain anchor to check.

**This design.** Same operational shape as Vanta (owner-published checkpoint + auto-sync
delta), but **trust-minimized**: the on-chain commitment set is the canonical index, the
checkpoint only supplies blob *content* for that index, and every follower **verifies each
blob against its on-chain hash + drand and re-grades deterministically**. The publisher can
only *withhold* (which maps to a uniform forfeit), never forge a signal or a grade.

---

## Core principle

```
on-chain commitments  =  canonical INDEX  (what/when, immutable, everyone reads the same)
checkpoint/archive     =  blob CONTENT for that index  (the only thing off-chain)
follower               =  verify each blob vs its on-chain hash + drand, then RE-GRADE
```

Everything except the blob is already replayable: commitment hash + `commit_block` (T0) on
chain; drand signatures public/permanent; `bands_as_of(T0)` committed in code; price bars
historically stable. So the checkpoint is, in essence, **a durable, complete blob archive** —
not a trusted statement of standings.

---

## 1. Checkpoint format (`.json.gz`)

Grades and derived state are **NOT** authoritative in the file — followers recompute them.
The file carries only what isn't trivially on-chain: the blobs (+ pointers) and a drand cache.

```jsonc
{
  "schema": 1,
  "netuid": 89,
  "created_ms": 1750000000000,
  "covers_through_block": 8500000,        // follower learns how far this snapshot reaches
  "drand": { "29955017": "<sig_hex>", … },// optional cache (re-fetchable from drand API)
  "signals": [
    {
      "commit_hex":   "4f77a7…",          // on-chain commitment hash = INDEX KEY
      "hotkey":       "5CktCPeg…",
      "commit_block": 8412345,            // on-chain inclusion block (= T0); a hint, re-checked
      "round":        29955017,           // drand round (hint, re-checked vs commit+24h window)
      "nonce":        "…",
      "blob":         { … env_A tlock ciphertext … },   // THE off-chain content
      "blob_first_seen_ms": 1749990000000 // when the canonical archive first received it
    }
    // … one entry per blob the archive holds …
  ]
}
```

Notes
- **No `status`/`outcome`/`weight`/`eliminated_t0`/`strikes` fields are trusted.** They are
  deterministic functions of the journal; followers derive them (`scoring.elimination_t0`,
  the strike logic, `compute_weights`). Including them is allowed only as a non-authoritative
  cross-check log.
- `first_seen_unix` (immunity clock) is **not** in the file — it is the block time of a
  hotkey's first on-chain commitment, which the follower reads from chain.
- Delta form: `GET …?since_block=N` returns only `signals[]` whose `commit_block > N`.
- `referrals[]` (§ referral incentive): raw journaled claims
  `{recruiter_hk, recruit_hk, commit_block, recruit_reg_block}`. `commit_block` is the
  on-chain inclusion block of the recruiter's `sn89ref:1:<recruit_ss58>` commitment
  (anchor-checkable via `audit_journal.py --referral-anchors`); `recruit_reg_block` is
  the recruit's registration block at the validator's FIRST metagraph sighting (null
  until the recruit registers). Validity, the one-recruiter-per-recruit rule, the
  breadth cap, and the pair no-copy suspension are all **re-derived** by
  `replay.weights_from_journal` — never trusted from the file. Inert while
  `REFERRAL_ENABLED=0`; old auditors ignore the key.

---

## 2. Follower verification + re-grade (per signal)

For every commitment the follower discovers on-chain (see §3), it does **exactly the live
reveal/grade path** — no new trust:

1. **Obtain blob**: `checkpoint[commit_hex]` → else relay `bucket.fetch(blob_url)` → else
   **no content anywhere ⇒ forfeit `LOST`** (per §4).
2. **drand**: ensure `round` matured; get its signature (`_drand_sig`, re-fetchable from the
   public drand API; a cached checkpoint sig is verified against the drand public key).
3. **Decrypt + bind**: `crypto.decrypt_timelock(blob, sig)` → plaintext; require
   `SHA256(plaintext‖salt) == commit_hex` (the existing AEAD/commitment binding). Mismatch ⇒
   reject blob (strike), treat as no valid blob.
4. **Window**: `crypto.expected_round_ok(round, t0)` (commit+24h) — else `void`
   (`round_out_of_window`), the existing rule.
5. **Re-grade**: `config.bands_as_of(commit_block_time)` + the price feed at `t0 + horizon`
   → `won|lost|washed`. Deterministic; the checkpoint's opinion of the outcome is ignored.

The follower's journal row is thus *derived and verified*, identical to what an incumbent
produced live — because all five inputs are identical (on-chain T0, on-chain hash, public
drand, committed bands, stable price). The publisher cannot inject a fake signal (no on-chain
`commit_hex` ⇒ ignored) nor flip a grade (re-graded locally).

---

## 3. Genesis commitment index (the other half — must ship with §1–2)

The checkpoint supplies content; the **chain** supplies the authoritative *set*. A follower
must enumerate **all** commitments since subnet genesis, not just the latest-per-hotkey
snapshot. Fix the ingest cursor:

- `SCAN_COMMITMENT_HISTORY = 1` on mainnet (today defaults `0`).
- Initialize the scan cursor to the **subnet registration block**, not `block-1`
  (`_last_scanned_block` currently falls back to `block-1` for a fresh DB).
- Loop `read_commitments_in_block_range` past the `SCAN_MAX_BLOCKS_PER_POLL=120` cap until
  caught up to head (catch-up mode), then resume incremental polling.

The union {on-chain `commit_hex`} is the ground truth of what must be accounted for. Any
on-chain commitment with **no verifying blob anywhere** is a deterministic forfeit — the
publisher cannot hide a loss by omitting a blob, because the commitment is still on chain and
everyone forfeits it identically.

---

## 4. Forfeit determinism (pin this decision)

Today forfeit is per-validator wall-clock: "blob not captured by `reveal+grace` ⇒ LOST,"
which a late validator cannot re-observe. Two replayable options:

- **(A) Archive-anchored deadline.** Gradeable iff a verifying blob exists with
  `blob_first_seen_ms ≤ round_time(round) + REVEAL_GRACE_S`, where `first_seen` is the
  canonical archive's recorded receipt time. Replayable from the checkpoint; preserves the
  current "serve on time or forfeit" semantics. Trust point: the archive's `first_seen`.
- **(B) Relaxed — recommended.** Gradeable iff *any* verifying blob exists in the canonical
  archive, ever; never ⇒ `LOST`. Fully replayable, no timing race, no trusted timestamp.
  Safe against the original gaming concern: the outcome is fixed by the **on-chain T0 +
  horizon**, so serving a blob late cannot cherry-pick; a withheld loser is still forfeited
  `LOST` (same cost as revealing). The only thing late-serving changes is *when* the row
  resolves, not its outcome.

Recommend **(B)** for clean cross-validator determinism. It is a CONSENSUS change → all
validators upgrade in lockstep.

---

## 5. Auto-sync mechanism (Vanta-modeled, untrusted source)

- **Canonical archive endpoint** (owner runs it; any validator may mirror it):
  `GET /sn89/checkpoint?since_block=N` → gz delta of `signals[]` with `commit_block > N`.
  An `api_key` is **rate-limiting only** — the content is self-verifying, so the endpoint is
  untrusted; a follower may pull from *any* mirror and reach the same journal.
- **Follower loop**: every K minutes, request `since_block = last_covered`, run §2 over the
  delta (skip `commit_hex` already journaled — `compute_delta` style), advance the cursor.
- **Source role**: the owner/archive validator captures blobs live and serves them; it does
  not sync from anyone (the `is_mothership`/`auto_sync = … and not is_mothership` pattern).
- **Robustness**: because verification makes the source untrusted, encourage every validator
  to (a) persist every blob it captures and (b) optionally re-serve — turning the single
  owner archive into a gossip mesh. No single point of trust; single points of *availability*
  are mitigated by mirrors.

---

## 6. Why this beats Vanta

| | Vanta / SN8 | SN89 (this spec) |
|---|---|---|
| Catch-up source | mothership JSON | owner checkpoint **or any mirror** |
| Trust | blind (trust Taoshi) | **verify each entry vs on-chain hash + drand, re-grade** |
| Forge a signal | possible | impossible (no on-chain `commit_hex` ⇒ ignored) |
| Forge a grade | possible | impossible (re-graded locally) |
| Worst-case publisher abuse | arbitrary | **withhold only** → uniform forfeit for all |
| Anchor | none | the chain |

---

## 7. Implementation phasing (each testnet-soaked before mainnet)

1. **Genesis index scan** (§3): cursor → registration block, uncap catch-up loop, default
   `SCAN_COMMITMENT_HISTORY=1` on mainnet. Verify a fresh DB rebuilds the full commitment set.
2. **Durable complete archive**: persist every captured blob `commit_hex → {blob, first_seen}`
   append-only, full history (replace the last-50 relay index with a complete `since_block`
   query). This is the band-history-style consensus artifact for blobs.
3. **Checkpoint endpoint** (§1, §5): `since_block` gz delta.
4. **Follower auto-sync + verify** (§2): reuse `reveal`/`grade_revealed`/`crypto.*`; only the
   *blob source* changes (checkpoint when relay misses).
5. **Forfeit rule** → option (B) (§4). Consensus change, lockstep.
6. **Golden-vector test**: two validators — one from genesis, one started late + auto-synced —
   must produce **byte-identical** journal and `compute_weights` output over a fixed fixture.

---

## 8. Residual non-chain dependency (unchanged by this spec)

Grading still reads an external **price feed**. All validators must use a feed that returns
identical historical bars for `(pair, t0, horizon)`; pin the provider and the as-of query
semantics. This is the one consistency dependency that is neither on-chain nor in the
checkpoint — same as today — and the only place two correct, fully-synced validators could
still diverge. drand (public/permanent) and bands (committed) are not concerns.
