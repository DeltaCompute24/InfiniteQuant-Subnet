# Caller collateral — design

Callers post SN89 alpha as collateral to earn emissions. Crossing the
elimination floor burns the collateral and zeroes the hotkey permanently.
Hotkeys without collateral can submit and build a track record but earn the
dust weight only.

The custody architecture is modeled on the SN8 (Taoshi/Vanta) collateral
system, which our callers already know as miners there. The contract and chain
mechanics are our own implementation of the same design; the slash policy is
SN89-specific.

## Why

Without collateral, a new caller's optimal strategy is variance: register many
hotkeys, keep the lucky one, abandon the rest. Track-record gates alone cannot
price that option. Collateral makes every hotkey cost real capital, makes
sustained bad signal destroy that capital, and — because slashes are burned —
turns bad callers into deflation for every alpha holder.

## Components

```
caller coldkey ──transfer_stake──▶ vault coldkey (alpha custody, owner-held)
                                       │
custodian (neurons/custodian.py) ──────┼── burn_alpha on slash (verifiable)
  holds: vault coldkey, EVM owner key  │
                                       ▼
                          EVM ledger (contracts/Collateral.sol)
                          public balanceOf / slash history / events
                                       ▲
validators (read-only) ────────────────┘  gate weights on balance_of(hotkey)
```

- **Vault coldkey** — a wallet controlled by the subnet owner. All posted
  alpha is staked under it. After a deposit, the alpha remains staked on the
  *caller's* hotkey (transfer_stake changes the owning coldkey, not the
  hotkey), so `vault stake on hotkey == ledger balance of hotkey` is an
  auditable on-chain invariant per caller.
- **Ledger contract** (`contracts/Collateral.sol`) — bookkeeping only, holds
  no funds. Owner-written, world-readable. Deployed behind a UUPS proxy on
  the Bittensor EVM. API-compatible with the SN8 ledger.
- **Custodian** (`neurons/custodian.py`) — owner-side CLI that executes every
  custody event and mirrors it into the ledger. Validators never hold keys.
- **Validator** — reads `balanceOf` per hotkey at each weights tempo and
  computes eliminations deterministically from its graded journal.

## Trust model — stated plainly

This is custodial, exactly like SN8's: the owner's vault coldkey holds the
alpha and the owner's EVM key writes the ledger. What the chain gives every
participant is **auditability**, not trustlessness:

- posted balances and the full deposit/withdraw/slash event history are public;
- every slash is paired with an on-chain `burn_alpha` — anyone can verify the
  alpha was destroyed, not pocketed;
- the per-caller invariant (vault stake on your hotkey == your ledger balance)
  is checkable by the caller at any time.

## Flows

### Deposit
1. Caller: `custodian.py make-deposit --amount 100 --wallet.name w --wallet.hotkey h`
   → coldkey-signed `transfer_stake` (destination = vault), printed as hex.
   Nothing is submitted; the caller's keys never leave their box.
2. Caller sends the hex to the owner.
3. Owner: `custodian.py deposit --extrinsic-hex …` — validates the call is
   exactly a transfer_stake of SN89 alpha to our vault, submits it, reads the
   credited amount from the chain's `StakeAdded` event, and credits the ledger.

### Elimination + slash
1. `scoring.elimination_t0` scans a hotkey's decisive history in t0 order.
   At each decisive signal it evaluates, using that signal's t0 as "now":
   - lifetime decisive ≥ `ELIM_MIN_DECISIVE` (20), and
   - trailing decisive in `SCORE_WINDOW_S` ≥ `ELIM_MIN_TRAILING` (10), and
   - trailing hit-rate < `ELIM_FLOOR_HIT` (0.40).
   The first crossing eliminates, terminally. Because the scan reads only the
   graded journal (no wall clock), every validator reaches the identical
   verdict no matter when it evaluates.
2. Validators mark `hotkey_meta.eliminated_t0` and exclude the hotkey from
   weights from the next tempo (zero, not dust).
3. Owner: `custodian.py slash-eliminated --execute` — debits the ledger
   (`slash`) and burns the same amount from the vault stake (`burn_alpha`).
   `ELIM_SLASH_PROPORTION = 1.0`: the full posted collateral burns.
4. An eliminated hotkey never scores again. Coming back means a new hotkey
   and fresh collateral — each life costs capital.

### Withdraw
1. Caller: `custodian.py make-withdraw-request …` → canonical JSON
   (sorted-key, no whitespace) signed by the coldkey, with nonce + timestamp.
2. Owner: `custodian.py withdraw --request r.json` verifies, in order:
   signature; freshness; coldkey owns hotkey (chain `Owner` storage); hotkey
   not eliminated; **settlement lock** — zero unsettled signals
   (sealed/revealed/pending) and `WITHDRAW_COOLDOWN_S` (72 h, the max signal
   horizon) elapsed since the last signal's t0; amount ≤ ledger balance.
3. Ledger debit, then `transfer_stake` vault → caller coldkey. If the chain
   transfer fails after the debit, the ledger is re-credited so books match.

The settlement lock is what prevents pull-before-the-floor-catches-you: a
caller cannot exit while any signal that could eliminate them is still open.

## Weight gating

`scoring.compute_weights` (consensus):

| state | weight |
|---|---|
| eliminated | excluded entirely (zero) |
| strikes ≥ limit | excluded (existing rule) |
| immune (first 8 d) | dust |
| collateral < `COLLATERAL_MIN_ALPHA` | dust |
| qualified + funded | pro-rata trailing wins |

Gating activates only when `SN89_COLLATERAL_CONTRACT` is set; until then
behavior is exactly pre-collateral (this PR is inert in production). If the
ledger RPC is unreachable at a weights tempo, the gate is waived for that
tempo — an outage must not dust every funded caller.

## Parameters (proposed — review these)

| constant | value | rationale |
|---|---|---|
| `COLLATERAL_MIN_ALPHA` | 100 alpha | env-overridable; calibrate so min collateral exceeds the expected value of a lucky-immunity emissions run |
| `ELIM_MIN_DECISIVE` | 20 | matches the qualification gate; floor can't trigger on a new caller's noise |
| `ELIM_MIN_TRAILING` | 10 | a hit-rate over fewer than 10 decisive is statistically meaningless |
| `ELIM_FLOOR_HIT` | 0.40 | absolute floor, well below the 0.52 qualification gate — a 45–50% cold streak earns nothing but survives; sustained sub-40% is destroyed |
| `ELIM_SLASH_PROPORTION` | 1.0 | terminal crossing = full burn; no partial bleed of good callers on routine losses |
| `WITHDRAW_COOLDOWN_S` | 72 h | max horizon — every signal that could eliminate has settled |

Design choices already made deliberately (context for review):
- **Absolute floor, not rank-based** — a caller can always compute their own
  distance to the line; nobody is slashed because someone else improved.
- **Burn, not redistribute** — slashed alpha is destroyed, not paid to
  winners; no PvP incentive to hunt other callers, and the token holders
  capture the penalty.
- **Floor below the qualification gate** — between 0.40 and 0.52 a caller
  earns nothing (fails to qualify) but keeps their collateral; only sustained
  performance below the floor is terminal.

## Open questions for v2

- **Gamble-for-resurrection**: a caller just above the floor holds nearly-dead
  collateral and is incentivized to swing for variance. Options: freeze new
  signal acceptance within a buffer of the floor, or scale the slash with
  depth below it.
- **Conviction sizing**: let collateral-at-risk per signal scale with declared
  size, making conviction measurable (the capital-weighting prediction markets
  get for free).
- **Decentralizing the custodian**: multisig on the EVM owner key / vault, or
  a collateral pallet if subtensor ever ships one.

## Deployment checklist

1. Create the vault wallet; store the mnemonic per the ops runbook (never in
   the repo or on the validator box).
2. Create the EVM owner EOA; fund it with gas TAO on the Bittensor EVM.
3. Deploy `Collateral.sol` behind a proxy (see `contracts/README.md`) —
   testnet (chainId 945, netuid 496) first.
4. Set `SN89_COLLATERAL_CONTRACT`, `SN89_VAULT_COLDKEY` for validators;
   additionally `SN89_OWNER_EVM_ADDRESS/KEY`, `SN89_VAULT_WALLET` for the
   custodian.
5. Run a full deposit → eliminate → slash-burn → withdraw cycle on testnet
   with a sacrificial hotkey before announcing.
6. Announce the requirement with a funding deadline; existing callers fund
   during a grace window before the dust gate activates.
