# Collateral v2 — escrowed, VotingPower-governed, swappable disposition

This is a **proposed redesign** of `contracts/Collateral.sol`, for review. It is
not wired into the subnet yet. It directly answers the *"decentralizing the
custodian"* open question in `docs/collateral.md`.

## Why the current ledger is being replaced

`contracts/Collateral.sol` holds **no funds**. The alpha sits staked on an
owner-held vault coldkey, and the contract only mirrors owner-executed
deposit/withdraw/slash events. It enforces nothing on-chain:

- the owner can write any balance, or none;
- nothing couples a ledger row to the actual staked alpha;
- a miner cannot trustlessly exit — `withdraw` is `onlyOwner`;
- "every slash is paired with a burn" is a promise, not an invariant.

It is a public audit log of events the operator *chooses* to emit.

## What v2 changes

Collateral is held as **escrowed TAO** (native value in the contract) and three
properties become on-chain invariants.

### 1. Real custody
`deposit()` locks value in the contract. `reclaimCollateral` → wait
`DECISION_TIMEOUT` → `finalizeReclaim` pays the miner, unless validators vote to
deny in the window. A miner can always exit absent an active objection.

### 2. Slashing governed by VotingPower, not a single trustee
No owner/trustee can seize collateral. A slash (and the denial of a reclaim) is
a **proposal that executes only when validators holding ≥ `quorumBps` of
snapshotted voting power vote yes**:

- voting power comes from a swappable `IVotingPower` source. The default
  `ValidatorVotingPower` mirrors the SN89 validator set's stake, synced by the
  owner each weights tempo. It can later be replaced — without migrating
  collateral — by a source that reads stake live from the Metagraph precompile.
- total voting power is snapshotted at proposal creation so stake shifting
  mid-vote can't move the bar.
- `quorumBps` is the **#1 parameter to review**. It defaults to a 2/3
  supermajority (`6667`) in the deploy example because the action destroys (or
  redirects) miner capital; lower it toward the consensus/qualify gate if you
  prefer a simple majority.

### 3. Disposition decoupled from the slash decision — the burn → SN8 switch
A slash moves collateral into a contract-held `seized` pool **and stops there.**
It does not burn. Where seized funds go is a separate, governance-controlled
`disburseSeized` routed through a swappable `ISlashTarget`:

- **today:** `slashTarget = BurnTarget` → seized collateral is destroyed
  (reproduces the current burn policy).
- **soon:** deploy an SN8 staking adapter implementing `ISlashTarget`, then
  `setSlashTarget(sn8Adapter)`. Seized collateral from an eliminated SN89 miner
  becomes subnet-8 trading capital instead of being burned.

The switch is **one governed call. `CollateralVoting` itself never changes.**
`test/CollateralVoting.t.sol::test_SwitchToSN8StakeTarget_NoCollateralChange`
proves seized funds route to a new target with no change to collateral accounting.

## Roles

| role | can | cannot |
|---|---|---|
| miner | deposit, reclaim, finalize own reclaim | seize, deny |
| validator (`votingPower > 0`) | propose/vote slashes and denials | move funds directly, change params |
| governance (multisig/timelock) | swap voting source / slash target, set `quorumBps`, disburse the seized pool | seize a specific miner's collateral (only a vote can) |

Governance is intentionally limited: it manages *parameters and the seized
treasury*, never an individual seizure. Making disbursement and parameter
changes themselves vote-gated is the natural next step.

## Files

| file | role |
|---|---|
| `CollateralVoting.sol` | escrow + reclaim + slash/deny voting + seized pool |
| `IVotingPower.sol` | voting-power source interface |
| `ValidatorVotingPower.sol` | default owner-synced voting-power source |
| `ISlashTarget.sol` | disposition-target interface |
| `BurnTarget.sol` | v1 disposition (destroy) |
| `test/CollateralVoting.t.sol` | Foundry suite (15 tests) |

## Run the tests

```bash
cd contracts/voting
forge install foundry-rs/forge-std --no-git
forge test -vv
```

## Open review questions

- **`quorumBps`** — supermajority vs. the consensus/qualify gate. Set deliberately.
- **`DECISION_TIMEOUT`** — must exceed the realistic time for a deny vote to reach
  quorum, since on-chain a deny vote is the only thing that can stop a reclaim.
  The off-chain settlement lock (no exit while signals are open) is still the
  validators' call on *when* to deny.
- **Voting-power source** — owner-synced mirror now; Metagraph-precompile source
  later. Confirm the precompile exposes per-validator stake on netuid 89.
- **Burn semantics** — `BurnTarget` sends to `address(0)`, which is unspendable
  on the EVM but not supply-reducing. If the Bittensor EVM exposes a true burn
  precompile, point `BurnTarget` at it; the interface is unchanged.
- **Deposit identity** — escrow credits the depositing EVM key and only that key
  can reclaim. Confirm how a miner's hotkey/coldkey maps to the EVM account that
  deposits and exits on the Bittensor EVM.
- **SN8 staking adapter** — out of scope here; it is the one new contract to
  build when the first SN89 miner is eliminated. `disburseSeized` only needs its
  address.
- **Upgradeability** — v2 is immutable (like the church reference). The live
  `Collateral.sol` is UUPS. Decide whether to put `CollateralVoting` behind a
  proxy.
