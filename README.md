# InfiniteQuant Subnet — SN89

Bittensor subnet 89. Miners commit encrypted directional trade calls. Validators grade them against real market data. Emissions go to miners with statistically proven edge.

> **Requires Python 3.10.** The `timelock` dependency publishes wheels only for 3.7–3.10. On 3.11/3.12: `apt install python3.10 python3.10-venv` (or pyenv) and build the venv with `python3.10`.

## Protocol

1. **COMMIT** — pick a pair + direction. The signal is encrypted with a 2-hour [drand](https://drand.love) timelock; `SHA256(signal)` goes on-chain via `set_commitment`. The commit block is the signal's timestamp. Entry price = open of the first 1-second bar at or after that block's time — every validator derives the same entry.
2. **REVEAL** — after 2 hours the timelock opens. Validators verify the plaintext against the on-chain hash, then grade on 1-minute candles: TP touched first = **WON** · SL touched first = **LOST** · neither by the horizon = **WASH**.
3. **EARN** — emissions ∝ trailing-7-day qualified wins (capped at 20) × hit-rate tier.

- A committed blob that never reveals is a decisive **LOSS**. Committing costs what revealing costs.
- Candles are bad-tick sanitized (uncorroborated wicks clamped; crypto cross-checked against Hyperliquid). Forex/metals rollover bars [20:55–21:20 UTC] are dropped from grading.

## Rules

| Rule | Value |
|---|---|
| Signals per UTC day | **3** per hotkey (from 2026-07-18T00:00:00Z; 6 before) |
| Min gap between calls | **1 h** (from 2026-07-18T00:00:00Z; none before) |
| TP / SL | per-asset, symmetric 1:1 — `data/signals-bands.json` |
| Grade window | crypto 8 h · forex/metals 12 h · equities 48 h |
| Same-pair repeats | allowed — including long-then-short on one pair |
| Unrevealed blob | LOSS 6 h past reveal round |
| Bad blob (hash/decrypt fail) | strike — 3 strikes in 30 d zeroes the hotkey 30 d |

Limits are as-of versioned (`config.SUBMISSION_RULES_HISTORY`): each signal is judged by the rules in force at its own commit time. A rules change never retroactively voids older signals. The miner tooling enforces the limits locally, so an over-limit call is refused with a reason instead of silently voiding.

## Scoring

```
decisive    = WON + LOST   (washes and voids never count)
reputation  = decisive trades in the last ~60 days, capped at the most recent ~100
QUALIFIED   = window decisive ≥ 8  AND  Wilson lower bound on true hit-rate ≥ 50%
              (~90% confidence you beat a coin flip — luck does not qualify)
tier        = on a SHRUNK hit-rate (thin samples pulled toward 50%):
              QUALIFIED ≥ 55% → 1.0×  |  SHARP ≥ 60% → 1.2×  |  WOLF ≥ 70% → 2.0×
your weight ∝ (min(wins last 30 d, 20) × tier) / Σ same over all qualified
```

- Reputation decays (~60 d / ~100 trades): old wins can't mask bad recent trading, and a bad early stretch ages out.
- Wins decay linearly over 7 days and are capped at 20 — conviction outweighs volume.
- **Warmup:** first 8 days = dust weight. Warmup trades build the record; warmup wins never pay. Post fresh wins after warmup to earn.
- If no qualified miner has recent wins, emissions burn.

## Copy penalty

- The first eligible hotkey on a `(pair, direction)` is the original; a different hotkey opening the same trade while it is live lands second.
- Landing second on ≥ 50% of your decisive trades (min 5) + a sharp 1:1 shadowing signature = habitual copier. Copied wins stop counting toward hit-rate.
- The first mover is always safe. Occasional overlap on a crowded trade is fine — the penalty targets systematic following.
- Leader eligibility (≥ 10 decisive, not eliminated, not holding both directions of a pair) blocks throwaway hotkeys from manufacturing copy-flags.

## Referral

- An existing miner can claim a **new** hotkey before it registers: `python neurons/miner.py refer <new_hotkey_ss58>` — commit the claim, wait for it to appear in the public checkpoint (~5 min), **then** register the recruit.
- While both hotkeys are earning: recruit +10% emissions, recruiter +20% of each recruit's score (capped at 100% of own score).
- A hotkey that registered before the claim can never be claimed. Adding a second miner? Claim it first.
- Strict pair no-copy: shadowing inside the pair suspends the bonus 30 days.

## Assets

| Class | Pairs |
|---|---|
| Crypto | BTCUSD · ETHUSD · SOLUSD · XRPUSD |
| Metals | XAUUSD · XAGUSD |
| Forex | AUDUSD · EURUSD · GBPUSD · NZDUSD · USDCAD · USDCHF · USDJPY |

Bands are per-asset, volatility-scaled, symmetric 1:1. A signal is graded against the band in force **at its commit block** (`data/signals-bands.json` + `data/signals-bands-history.json`) — a band update never changes an in-flight signal.

---

## Mine

```bash
# 1. install
git clone https://github.com/DeltaCompute24/InfiniteQuant-Subnet && cd InfiniteQuant-Subnet
python3.10 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 2. register your hotkey (~0.05 TAO burn)
btcli subnet register --netuid 89 --wallet.name mywallet --wallet.hotkey miner

# 3. get a token — DM  /token  to @Iqsignals2026_bot
export SN89_FEED_TOKEN=<token>

# 4. run (pick one mode)
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner follow   # mirror your IQ Signals bot/extension calls
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner serve --port 8089   # REST intake
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner submit --pair BTCUSD --direction LONG   # one-shot
```

- No market-data subscription needed — validators handle all pricing.
- The token scopes the feed to your own calls and authorizes the blob relay. Non-custodial: keys never leave your box.
- **One feed = one hotkey.** Mirroring the same feed from a second hotkey flags the duplicate as a copier.
- **Second miner? Claim it before registering** — see Referral above.
- Prefer auto-update (validators must track releases; miners should too):

```bash
bash run.sh miner follow --wallet.name mywallet --wallet.hotkey miner
```

### Blob hosting

Signals are encrypted locally and served at a public URL for validators. The transport is untrusted — integrity is the on-chain commitment. Default is the **owner relay** (zero setup, activated by `SN89_FEED_TOKEN`; blobs are pinned at submit, so a hosting outage can never cause a forfeit). Alternatives:

```bash
# your own S3/R2 bucket
export SN89_R2_ENDPOINT=https://<account>.r2.cloudflarestorage.com
export SN89_R2_BUCKET=<bucket>
export SN89_R2_ACCESS_KEY_ID=… SN89_R2_SECRET_ACCESS_KEY=…
export SN89_R2_PUBLIC_BASE=https://<public-bucket-host>

# local disk + static server (testnet)
export SN89_BLOB_DIR=$HOME/.sn89/blobs
export SN89_R2_PUBLIC_BASE=http://<your-host>:8799
```

### Link your X handle

```bash
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner register-x --handle @yourname
```

Shows your handle on the public leaderboard. Signed locally; keys never leave the box.

### Emissions & elimination

No collateral, no deposit — weight is earned on track record:

| State | Result |
|---|---|
| Immunity (first 8 days) | dust weight |
| Below the confidence gate | no emissions |
| Qualified (≥ 8 decisive, Wilson LB ≥ 50%) | emissions ∝ capped trailing-7d wins × tier |
| Lifetime hit-rate confidently < 45% (≥ 40 decisive) | **eliminated** — hotkey zeroed permanently |

Elimination is a lifetime confidence test, separate from the (reversible) qualify gate: a cold streak drops you to zero emissions until you recover; only a durably sub-floor record is removed.

### Testnet (netuid 496)

Same code, free TAO:

```bash
export SN89_NETWORK=test SN89_NETUID=496
btcli wallet faucet --wallet.name mywallet --subtensor.network test
btcli subnet register --netuid 496 --wallet.name mywallet --wallet.hotkey miner --subtensor.network test
```

Reveals still take 2 h; grading follows each call's horizon.

---

## Validate

Requirements:

1. **Market data** — a paid [Massive](https://massive.com) (Polygon) plan: Currencies + Crypto feeds, 1-second and 1-minute aggregates. The free tier cannot grade.
2. **Validator permit** — the hotkey must be staked into the validator set, or weight commits never reveal.

```bash
export POLYGON_API_KEY=…
bash run.sh validator --wallet.name myvali --wallet.hotkey vali    # auto-updater (recommended)
```

State: `~/.sn89/validator.db`. Grading is deterministic — same chain + same market data ⇒ same weights. **Run the current release**: grading code is consensus; a stale validator diverges and loses VTRUST.

### Timelock version compatibility

`timelock==0.0.1.dev0` (this repo's pin) and `0.0.2.dev0` produce incompatible ciphertexts. The validator opens both via a sidecar venv holding the *other* version:

```bash
python3.10 -m venv .venv-tl001 && .venv-tl001/bin/pip install "timelock==0.0.1.dev0"
export SN89_TLD_FALLBACK_PYTHON=$PWD/.venv-tl001/bin/python
```

### Delegate via child-key (recommended)

Skip the market-data plan — delegate to the authoritative validator:

```bash
btcli stake child set --netuid 89 \
    --wallet.name <your-wallet> --wallet.hotkey <your-validator-hotkey> \
    --children <AUTHORITATIVE_HOTKEY_SS58> --proportion 1.0
```

### Audit the validator

The authoritative validator publishes its journal as a public checkpoint. Anyone can replay it:

```bash
python3 scripts/audit_journal.py <CHECKPOINT_URL> --chain --anchors --referral-anchors
```

Re-derives the weight vector from the journal and confirms it matches on-chain. See `docs/single-validator-model.md`.

---

## FAQ

**Why can't I set my own TP/SL?** Symmetric fixed bands make hit-rate a clean skill metric; vol-scaling keeps the bar equivalent across assets.

**What if my blob is unreachable when validators poll?** They retry every 30 s through reveal + 6 h grace, and a fetched blob stays pinned. Use the relay and this risk is zero.

**When do emissions arrive?** Weights update every tempo (~72 min). Your first non-dust weight lands once the gate is cleared — a handful of trades for a clear edge.

## License

MIT
