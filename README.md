# InfiniteQuant Subnet — SN89

Bittensor subnet 89 — trading-signals. Miners commit encrypted directional trade calls; validators grade them on real market data and distribute emissions to miners who prove consistent edge.

## How it works

1. **Commit** — a miner picks a pair + direction, encrypts the signal with a [drand](https://drand.love) 24-hour timelock, and records `SHA256(signal)` on-chain via `set_commitment`. The block the commitment lands in is the signal's timestamp; validators anchor entry at the open of the first 1-second bar at or after that block time + 30 s, so every validator derives the same entry price.
2. **Reveal** — after 24 hours the timelock opens; validators verify the plaintext matches the on-chain hash, then grade on 1-minute candles: first touch of TP wins, first touch of SL loses, no touch by the horizon is a wash.
3. **Earn** — emissions flow to qualified miners weighted by wins in the last 30 days × lifetime hit-rate tier.

**You must reveal what you commit.** A blob still unfetchable 6 hours after its reveal round is a decisive LOSS — there is no free option to hide losers.

**Touches must be real prices.** Candles are bad-tick sanitized: an uncorroborated spike wick is clamped unless a second independent feed (Hyperliquid, for crypto) confirms the level in the same minute. Forex/metals bars during the rollover window [20:55–21:20 UTC] are dropped from grading entirely; a real breach persists past the window and is caught on the next clean bar.

### Scoring

```
decisive    = WON + LOST   (washes and voids never count)
hit rate    = lifetime wins / lifetime decisive   (never resets)
QUALIFIED   = lifetime decisive ≥ 30  AND  lifetime hit rate ≥ 55%
tier        = QUALIFIED ≥ 55% → 1.0×  |  SHARP ≥ 60% → 1.2×  |  WOLF ≥ 70% → 2.0×
your weight ∝ (your wins in last 30 days × tier) / Σ same over all qualified miners
```

Quality is forever; pay is recent. Hit-rate never resets; wins decay after 30 days. If no qualified miner has recent wins, emissions burn.

**Warmup:** new hotkeys get 8 days of immunity with dust emissions. Warmup trades build your hit-rate record but warmup wins never pay emissions — you must post fresh wins after warmup ends to rise above dust.

### Rules

| Rule | Value |
|---|---|
| Max signals per UTC day | 3 per hotkey |
| Min spacing | 4 h between your own signals |
| TP / SL | per-asset, symmetric 1:1 — `data/signals-bands.json` |
| Horizon | crypto 30 h · forex/metals 12 h · equities 48 h |
| Overlap | one open signal per (pair, direction) per hotkey |
| Unrevealed blob | LOSS 6 h past reveal round |

Voided signals don't count against your daily quota. Exception: a blob that fails hash-verification or won't decrypt at reveal is a strike — three strikes in 30 days zeroes the hotkey for 30 days.

### Copy penalty

The first hotkey to open a `(pair, direction)` is the original. Any other hotkey opening the same trade while the original is live lands second. A hotkey whose landed-second trades exceed half its decisive trades is a habitual copier — its copied wins stop counting toward hit-rate, dragging it below the qualification gate. Occasional overlap is fine; systematic copying is strictly negative.

The mechanism is leader-agnostic: it counts how often you land second into anyone's live trade, not how often you follow one specific miner. Rotating victims doesn't help — your landed-second rate climbs all the same.

> The first mover is always safe. An honest miner who only occasionally lands second on a crowded trade keeps those wins — the penalty fires only once landed-second trades dominate your record (≥ 50%, min 5).

### Assets

13 assets across three classes:

| Class | Pairs |
|---|---|
| Crypto | BTCUSD · ETHUSD · SOLUSD · XRPUSD |
| Metals | XAUUSD · XAGUSD |
| Forex | AUDUSD · EURUSD · GBPUSD · NZDUSD · USDCAD · USDCHF · USDJPY |

Bands are per-asset volatility-scaled and symmetric 1:1. Crypto bands carry a ×1.75 unit multiplier and a longer 30 h grade window (vs 12 h for forex/metals). Versioned in `data/signals-bands.json`; a band update never retroactively changes an in-flight signal.

---

## Running a Miner

### Setup

```bash
git clone https://github.com/DeltaCompute24/InfiniteQuant-Subnet && cd InfiniteQuant-Subnet
python3.10 -m venv .venv && . .venv/bin/activate     # timelock wheel requires 3.10
pip install -r requirements.txt
btcli subnet register --netuid 89 --wallet.name mywallet --wallet.hotkey miner
```

Miners do not need a market data subscription — validators handle all pricing.

### Blob hosting

Your signal is encrypted locally and served at a public URL so validators can fetch it. The transport is untrusted (integrity is the on-chain commitment), so it doesn't matter where it lives. Pick one:

**1. Owner relay (default — zero setup).** Set `SN89_FEED_TOKEN` or `SN89_RELAY_TOKEN` and the miner pushes the encrypted blob through the IQ relay. No bucket, no server, no extra keys. We can't read it (24-hour timelock, bound to your hotkey) and can't forge one (hash is on-chain). Relay-hosted blobs are pinned at submit, so a hosting outage can never cause a forfeit.

**2. Your own S3/R2 bucket:**
```bash
export SN89_R2_ENDPOINT=https://<account>.r2.cloudflarestorage.com
export SN89_R2_BUCKET=<bucket>
export SN89_R2_ACCESS_KEY_ID=… SN89_R2_SECRET_ACCESS_KEY=…
export SN89_R2_PUBLIC_BASE=https://<public-bucket-host>
```

**3. Local disk + static server** (testnet / soaks):
```bash
export SN89_BLOB_DIR=$HOME/.sn89/blobs
export SN89_R2_PUBLIC_BASE=http://<your-host>:8799
```

### Interface 1 — Follow mode (Telegram bot / Chrome extension)

Already submitting calls through the IQ Signals Telegram bot or Chrome extension? Mirror them onto SN89 automatically. DM `/miner` to the Signals Bot for a feed token, then:

```bash
export SN89_FEED_TOKEN=<token from /miner>
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner follow
```

The token scopes the feed to your own calls and authorizes the blob relay — no bucket needed. The follower long-polls your submissions only and commits each one with your local hotkey the moment it appears. Non-custodial: the platform never sees your keys, and the token can't read anyone else's calls. The program's rules (3 calls/day, ≥ 4 h apart) match the subnet's rules exactly.

### Interface 2 — REST API

```bash
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner serve --port 8089
curl -s localhost:8089/submit -d '{"trade_pair":"XAUUSD","direction":"SHORT"}'
```

To expose beyond localhost, set a bearer token (the server refuses a public bind without one):

```bash
export SN89_INTAKE_TOKEN=<long random string>
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner \
    serve --host 0.0.0.0 --port 8089
curl -s https://<your-host>:8089/submit \
    -H "Authorization: Bearer $SN89_INTAKE_TOKEN" \
    -d '{"trade_pair":"XAUUSD","direction":"SHORT"}'
```

### Interface 3 — CLI one-shot

```bash
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner \
    submit --pair BTCUSD --direction LONG
```

### Link your X handle

Put your handle on the public leaderboard and get tagged in social posts:

```bash
python neurons/miner.py --wallet.name mywallet --wallet.hotkey miner \
    register-x --handle @yourname
```

Signs `sn89-register-x:<hotkey>:<handle>:<timestamp>` locally — keys never leave the box. Re-run any time to update your handle.

### Collateral

Post SN89 alpha as collateral to earn above dust weight (details: `docs/collateral.md`):

```bash
python neurons/collateral_cli.py deposit  --amount 100 --wallet.name my --wallet.hotkey my
python neurons/collateral_cli.py balance  --hotkey <ss58>
python neurons/collateral_cli.py withdraw --amount 100 --wallet.name my --hotkey <ss58>
```

| State | Result |
|---|---|
| No collateral | Dust weight (track record only) |
| 40–55% hit rate | No emissions, collateral kept |
| Below 40% over ≥10 decisive (after ≥20 lifetime decisive) | **ELIMINATED** — collateral burned, hotkey zeroed permanently |

Withdrawals settle after all open signals resolve plus a 72 h cooldown. Not yet active: gating turns on when `SN89_COLLATERAL_CONTRACT` ships in a release.

### Testnet (netuid 514)

Every command above works on testnet — same code, same protocol, free TAO, no risk:

```bash
export SN89_NETWORK=test SN89_NETUID=514
btcli wallet faucet --wallet.name mywallet --subtensor.network test
btcli subnet register --netuid 514 --wallet.name mywallet --wallet.hotkey miner \
    --subtensor.network test
```

Signals are still drand-timelocked — grading takes ~24 h on testnet too. A fresh testnet miner shows dust weight until its first revealed signals grade.

---

## Running a Validator

Two requirements before you start:

**Market-data subscription.** Validators need a paid [Massive](https://massive.com) (formerly Polygon.io) plan covering the Currencies feed (forex + metals) and the Crypto feed, with intraday aggregates — 1-second bars for entry anchoring and 1-minute bars for TP/SL grading across all 13 assets. The free tier cannot grade signals; without a sufficient plan your validator leaves signals stuck `pending` and weights diverge from consensus.

**Validator permit.** The hotkey must be staked into the validator set. Without a permit, commits pile up unrevealed until `TooManyUnrevealedCommits` and the validator earns nothing. The validator logs a warning and skips weight-setting in that state.

```bash
export POLYGON_API_KEY=…
python neurons/validator.py --wallet.name myvali --wallet.hotkey vali
```

State lives in `~/.sn89/validator.db` (SQLite). Grading is deterministic — same chain + same market data ⇒ same weights — so validators converge without coordination. Crypto bad-tick corroboration queries Hyperliquid's public candle API (no key needed). Entry timing design rationale: `docs/entry-timing.md`.

### Testnet

```bash
export SN89_NETWORK=test SN89_NETUID=514 POLYGON_API_KEY=…
python neurons/validator.py --wallet.name myvali --wallet.hotkey vali
```

---

## FAQ

**Why can't I set my own TP/SL?** Symmetric fixed bands make hit-rate a meaningful skill metric. Vol-scaled bands keep the bar equivalent across assets — an 18 bps FX cross and a 180 bps crypto are scored on the same relative standard.

**What if my blob is unreachable when validators poll?** They retry every 30 s through reveal + a 6 h grace. A blob fetched in that window grades normally and stays pinned even if you later remove it. A blob never served is a forfeit LOSS — use the owner relay to eliminate this risk entirely.

**When do emissions arrive?** Weights update every tempo (~72 min). Your first non-dust weight lands after your 30th decisive trade, assuming ≥ 55% hit rate.

---

## License

MIT
