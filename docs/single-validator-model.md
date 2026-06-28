# SN89 — Single-Validator Model (one authoritative validator, verifiable replay)

## Why one validator

SN89's scoring is a pure, deterministic function of the journal (on-chain commits +
drand + price + committed bands). We tried to make *many* independent validators
trustlessly reconstruct the same journal and it doesn't work in practice: the only
historical-commitment access is ~1 RPC/block (too slow to backfill in the hot loop),
and a second chain connection in a worker thread deadlocks `substrate-interface`.
That dead-ends the multi-validator catch-up problem.

The pragmatic answer — the same one Astrid/SN127 was advised toward by its own
owner-advisor ("you can move to a single validator… commit submission times to chain
or a bucket… miners should be able to replay and be certain the results match"):

> **Run ONE authoritative validator. Other would-be validators child-key / delegate
> to it. There is then one journal and no multi-validator sync problem at all.**

The catch with "one validator" is normally centralization. SN89 removes that catch,
because trust here comes from **replayability, not redundancy**.

## Where trust comes from (not "trust the operator")

Every input to a weight is public and permanent, so anyone can rebuild the weight
vector and check it:

1. **No fabricated signals.** Every graded signal is a `set_commitment` hash on-chain,
   signed by the *miner's* hotkey, at a consensus-exact block. The operator cannot
   invent signals or put them under a miner's key.
2. **No altered signals.** The encrypted blob is bound to the on-chain hash
   `SHA256(plaintext‖salt)`; a changed plaintext fails the hash and is rejected.
3. **No fudged grades.** Grades are re-derivable from the commit-block T0, the public
   price feed, and the committed bands-in-force — independent of the operator.
4. **No fudged weights.** The weight vector is a pure function of the journal
   (`sn89_signals/replay.py`), so anyone re-runs it and must get the same numbers.

This is **centralized operation, decentralized verification** — the opposite of a
trusted-checkpoint subnet (e.g. SN127/Astrid today), whose single source is a private
API with no on-chain anchor to check against.

## The audit (this is the whole point)

The authoritative validator publishes its journal as a checkpoint each cycle; anyone
re-derives the weights from it and confirms they match the chain.

```bash
# the validator (read-only on its DB), served at a public URL:
python3 scripts/export_checkpoint.py /var/www/sn89/checkpoint.json --chain

# any miner / skeptic / auditor:
python3 scripts/audit_journal.py checkpoint.json --chain
#   ✓ REPLAY MATCHES recorded weights      ← weights are an honest function of the journal
#   ✓ COMMIT ANCHORS: N/N match on-chain    ← no fabricated signals
#   AUDIT PASSED
```

`audit_journal.py` runs three checks, **out-of-process** (no validator role, no
deadlock) and with **targeted single reads** (no per-block scan):

- **Replay** (offline): re-derive weights from the journal with the *same* scoring
  code the validator runs (`replay.weights_from_journal`), compare to the recorded
  weights. Catches a hidden loss, a fudged tier, a mis-applied gate. The replay
  **re-derives eliminations and copy-flags itself** — it does not trust the
  validator's claims.
- **On-chain weights** (`--chain`): compare the replay to the *live metagraph*
  weights, catching a validator that set something different on-chain than its
  journal implies.
- **Commit anchors** (`--chain`): each signal's `commit_hex` must exist on-chain at
  its `commit_block` (single `CommitmentOf` reads) — no fabricated signals.

A mismatch is provable and public: the journal is signed/served, the weights are
on-chain, anyone reproduces the discrepancy. The operator's worst case is the same as
before — they can *withhold* a blob (→ a uniform forfeit `LOST` for everyone), never
forge, alter, or re-weight.

### Audit tiers (what each needs)
| Tier | Proves | Needs |
|---|---|---|
| Replay (weights vs journal) | weights are honest given the journal | nothing — just the checkpoint |
| Commit anchors | no fabricated signals | chain (read-only) |
| Re-grade (deeper) | the won/lost grades are honest | blobs + the price feed |

The re-grade tier (decrypt each blob, verify its hash, re-grade from price+bands) is
the checkpoint-sync design (`checkpoint-sync-spec.md`) applied as an audit rather than
a sync; it needs market data, so it's for validators/auditors who have a feed.

## Operating it

- **One validator** runs `neurons/validator.py` (the owner's, with a permit). It sets
  weights and publishes the checkpoint.
- **Other validators delegate** via Bittensor **child-hotkeys** (`btcli stake child …`)
  to the authoritative hotkey, rather than running divergent scoring. One environment,
  one journal, one weight vector — auditable by all.
- **No vault self-mining.** Weights go only to graded miners (pro-rata wins × tier),
  immune dust, and burn — there is no owner weight injection or API dial. The owner's
  own miners (the IQ traders) earn solely by track record, exactly like everyone else.

## Status / supersession

This **supersedes the multi-validator genesis-backfill** (PRs #32/#33), which is inert
(off by default) and deadlocks if enabled — recommend reverting it now that catch-up is
solved by single-validator + audit. The confidence scoring, band-versioning, and the
audit tooling here are the keepers.

The one unchanged dependency is the **price feed** (the re-grade tier re-fetches prices);
all validators/auditors must use the same provider + query semantics on finalized bars.
Named openly, because a fairness claim that hides its assumptions isn't one.
