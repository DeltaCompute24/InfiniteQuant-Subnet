# Proof: commitment-overwrite handling on testnet (audit #5)

**What this proves:** a miner cannot make a prior submission disappear from a
validator's record by overwriting its on-chain commitment. The two submissions
are captured and graded independently.

**Where:** live SN89 testnet, **netuid 496**, network `test`.
**When:** 2026-06-26, blocks 7429160–7429166.
**Validator:** `sn89-validator.service` on iq-main, running
`security/sn89-audit-hardening` with `SN89_SCAN_COMMITMENT_HISTORY=1`.
**Test hotkey:** `sn89test/miner2` = `5CktCPegg5u6yQ8VnykhKi89Wu4CWd3ALMyFqQm9u3kV4nR9`
(uid 3 — the testnet "coinflip adversary" miner).

The driver and verifier is `tools/soak_scan_commitment_history.py`.

---

## 1. The attack, staged

`miner2` committed **A** (`aaaa…`), then immediately **overwrote** it with **B**
(`bbbb…`). Substrate stores one commitment per hotkey (latest-wins), so after the
overwrite the chain's `CommitmentOf[miner2]` holds **only B**.

```
[3] Active overwrite proof (A then B inside one poll interval)
    committing A (aaaaaaaa…) as 5CktCPeg… at block ~7429160
    overwriting with B (bbbbbbbb…) immediately
    snapshot[5CktCPeg…] = bbbbbbbb…  scan = {aaaaaaaa, bbbbbbbb}
  [PASS] active overwrite — snapshot saw only B (the overwrite) but the scan
         recovered both A and B — the scan closes the race end-to-end
```

- **Old behaviour (`read_all_commitments_with_block`, latest-wins snapshot):**
  sees **only B**. Under the pre-fix code, A is lost.
- **Fixed behaviour (`read_commitments_in_block_range`, history scan):**
  recovers **both A and B**, each with its own consensus-exact inclusion block.

The back-to-back overwrite was **accepted** by the chain — this runtime exposes
no `Commitments.RateLimit` (only `MaxSpace=3100` bytes), so the overwrite race is
genuinely reachable here and the scan is the necessary mitigation, not just
defence-in-depth.

## 2. The live validator captured both

The production validator (flag on) journaled **both** commitments:

```
Jun 26 13:04:55  SN89 validator · netuid=496 · network=test · db=/root/.sn89/validator.db
Jun 26 13:11:32    + sealed aaaaaaaaaaaa… 5CktCPeg… round=29920837
Jun 26 13:12:33    + sealed bbbbbbbbbbbb… 5CktCPeg… round=29920837
```

Validator DB (`/root/.sn89/validator.db`), both rows persisted with distinct
consensus-exact `commit_block` values:

```
commit_hex        hk          round     commit_block  status
aaaaaaaaaaaaaaaa  5CktCPegg5  29920837  7429162       sealed
bbbbbbbbbbbbbbbb  5CktCPegg5  29920837  7429166       sealed   ← the overwrite
```

Two distinct rows ⇒ A and B grade independently on their own walk-forward
windows. The overwrite did not erase A.

## 3. The scan's event parsing was validated against the live runtime

The one thing a unit test cannot check — that `read_commitments_in_block_range`
recognises this runtime's actual `set_commitment` event shape — was confirmed by
the passive scan (5/5 events parsed, committer resolved on every one):

```
[1] Event-shape recognition
    block 7429073: Commitments.Commitment attrs={'netuid':…, 'who':'5EjT58o4…'} → account='5EjT58o4…'
    block 7429074: Commitments.Commitment attrs={'netuid':…, 'who':'5ELuTa45…'} → account='5ELuTa45…'
    …
  [PASS] set_commitment events recognized — 5 event(s), account resolved on all
```

## Two independent defences, both shown working

1. **Fixed-cadence polling** (default-on): the validator polled between A and B
   (13:11:32) and journaled A *before* B overwrote it — frequent, uniform polling
   shrinks the window in which an overwrite can be missed.
2. **Commitment-history scan** (`SCAN_COMMITMENT_HISTORY`): even reading *after*
   the overwrite — when the snapshot holds only B — the scan recovers A from its
   inclusion block. This is the backstop for any overwrite that lands between two
   polls.

## Reproduce

```bash
cd /opt/sn89-signals && set -a && . ./.env.test && set +a
.venv/bin/python tools/soak_scan_commitment_history.py \
    --network test --netuid 496 --active \
    --wallet.name sn89test --wallet.hotkey miner2
```
