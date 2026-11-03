# Agent competition v2 (agent books): go-live checklist

Branch `agent-books-v2` → `master` of `/opt/sn89-signals` on iq-main. Every step lists the
command, the gate, and the check that shows it worked. Do the steps in order and do not
skip a check.

## Dates used below (example; move all of them together)

| what | value | unix |
|---|---|---|
| HF anchor v2 arms (`SN89_AGENT_ANCHOR_V2_FROM`), a 180 s window boundary | 2026-11-15 00:00 UTC | `1794700800` |
| Intake opens + epoch + first scored week (`SN89_AGENT_BOOKS_FROM`), a Monday 00:00 UTC | 2026-11-16 00:00 UTC | `1794787200` |
| First frozen week (freeze timer) | 2026-11-23 06:05 UTC | |
| First payment, and the COMP_WEIGHTS row | 2026-11-23 08:00 UTC | `1795420800` |

Rules:
- The anchor must arm BEFORE the epoch. The freeze runs with `--require-anchor`, so any
  receipt whose window has no v2 anchor never counts.
- The same `SN89_AGENT_BOOKS_FROM` value goes in BOTH env files: the intake's and
  `/opt/sn89-signals/.env.main`. It is the intake's opening instant and the validator's
  epoch.
- systemd `EnvironmentFile` keeps a trailing `# comment` inside the value, so put no inline
  comments on these lines.

## 0. Preconditions

- [ ] **Intake + signer + droplet deployed** (Whit's script) with `SN89_AGENT_BOOKS_FROM=0`
      still in the intake env. Receipts will appear under
      `/opt/iq-platform/data/live/sn89-agent/books/` once it opens.
- [ ] **Bond**: the coldkey `5HCC5ENqaQa38uuVWxeqdpMP9dYbgebjVGtJdSShSKv4mGZH` holds at least
      10,000 alpha on netuid 89, counted across every hotkey. It held 2,374.35 at block
      9414501 (2026-11-02 00:00 UTC) and 2,380.39 on 2026-11-03. Below the bar, our books are
      paid 0. Check:
      ```bash
      ssh iq-main 'cd /opt/sn89-signals && .venv/bin/python -c "
      from sn89_signals import chain; c=chain.Chain()
      print(c.alpha_for_coldkey(\"5HCC5ENqaQa38uuVWxeqdpMP9dYbgebjVGtJdSShSKv4mGZH\", c.current_block()))"'
      ```

## 1. Back up

```bash
ssh iq-main
TS=$(date -u +%Y%m%dT%H%M%SZ)
cd /opt/sn89-signals
git tag pre-agent-books-v2-$TS && git push origin pre-agent-books-v2-$TS
cp -p .env.main .env.main.bak.agentbooks.$TS
git status --short | grep -v '^??'      # expect only: M scripts/closers_positions_publisher.py
git rev-parse HEAD                       # write it down
```

## 2. Merge to master (tree only: nothing restarts here)

```bash
cd /opt/sn89-signals
git fetch origin
git merge --no-ff origin/agent-books-v2 -m "Merge agent-books-v2: Agent competition v2 (both flags 0)"
```

Gates, all of which must pass before pushing:

```bash
.venv/bin/python -m py_compile neurons/*.py tools/*.py sn89_signals/*.py deploy/agent-books/publish_weeks.py
PYTHONPATH=. .venv/bin/pytest -q -p no:cacheprovider
# expect exactly 2 failures, both already failing on master:
#   test_markets_v2::test_collateral_cap_ignores_the_over_limit_bet
#   test_markets_v3::test_in_window_bet_is_filled_at_the_first_tick_after_receipt_plus_the_delay
git push origin master
git status --short | grep -v '^??'       # master == working tree apart from the closers file
```

With both flags at 0 the merged code commits the same vector and v1 anchors. No process
loads the new code until it restarts.

## 3. COMP_WEIGHTS_HISTORY row (master, same session)

Append to `sn89_signals/config.py` `COMP_WEIGHTS_HISTORY`, after the 2026-11-05 row:

```python
    # ⚑ 2026-11-23 08:00:00 UTC: Agent v1 (LF-graded calls) ends; its 0.25 goes to
    # `agentbook`, the frozen agent-books week (agent_books_io). The first frozen week is
    # 2026-11-16..23, paid from 08:00 on the 23rd. mecid-0-RELATIVE, chain split unchanged.
    (1795420800, "lf:0.375,agentbook:0.25,reserve:0.125,markets:0.25"),
```

Then run pytest (same gate as step 2), commit, and push. Check:

```bash
.venv/bin/python -c "from sn89_signals import config as c; print(c.comp_weights_as_of(1795420799), c.comp_weights_as_of(1795420800))"
```

## 4. Webhook routes (iq-platform side, `partner/web/sn89_api.py`)

1. `_HF_NAME` gains `"agent.jsonl"` and `"anchor-v2.json"`. Without them the v2 halves
   of each window 404 publicly, and no third party can verify a v2 anchor.
2. A sibling route serves frozen weeks:
   `GET /sn89/agent/weeks/{name}` from `/opt/iq-platform/data/live/sn89-agent/weeks/`,
   where name is `index.json`, `agent-books-week-YYYY-MM-DD.json` or `….json.sha256`.

Public URLs:
- `https://partner.infinitequant.app/sn89/agent/weeks/index.json`
- `https://partner.infinitequant.app/sn89/agent/weeks/agent-books-week-2026-11-16.json` and its `.sha256`

The intake's books route (`/sn89/agent/books/…`) is staged separately.

Deploy it the usual way: rsync, `.bak` copy, `py_compile`, `systemctl restart
iq-partner-webhook`, then record it:

```bash
python3 /opt/iq-platform/services/iq-routines/lib/ledger.py record-change --actor whit \
  --summary "sn89_api: serve HF agent.jsonl/anchor-v2.json + /sn89/agent/weeks" \
  --files apps/partner-webhook/partner/web/sn89_api.py
```

Check: `curl -s -o /dev/null -w '%{http_code}\n' https://partner.infinitequant.app/sn89/agent/weeks/index.json`
returns 404 until the first publish (step 7) and 200 after it.

## 5. Flags in `/opt/sn89-signals/.env.main`

```bash
cd /opt/sn89-signals
cat >> .env.main <<'EOF'
SN89_AGENT_ANCHOR_V2_FROM=1794700800
SN89_AGENT_BOOKS_FROM=1794787200
EOF
grep -n 'SN89_AGENT_' .env.main
```

Set `SN89_AGENT_BOOKS_FROM=1794787200` in the intake's env as well, via Whit's script.

## 6. Restarts: the HF anchor, then ONE batched validator restart

These two are the only restarts for the whole go-live. Do them together, any time before
2026-11-15 00:00 UTC.

```bash
systemctl restart iq-sn89-hf-anchor
journalctl -u iq-sn89-hf-anchor --since '-3 min' | tail -5       # "anchor hotkey 5FCN4P1K…", sweeping
# SN89 rate-limits weight commits (100 blocks): restart the validator ONCE.
systemctl restart sn89-validator-main
journalctl -u sn89-validator-main --since '-10 min' | grep -E 'combined weights|agentbook|set_weights' | tail
```

Expected validator lines:
- Before 2026-11-16: no `agentbook` line, because the flag is not active yet.
- From 2026-11-16 to 2026-11-23 08:00: `· agentbook week 2026-11-09: no frozen file (share
  burns) [unpaid: no share yet]`. The share key does not exist yet, so nothing burns.
- After 2026-11-23 08:00: `· agentbook week 2026-11-16: N paid account(s)`, and `agentbook`
  appears in `shares=`.

A `CommittingWeightsTooFast` right after the restart is harmless; see `~/IQ/CLAUDE.md`.

## 7. Prove anchor v2 (from 2026-11-15 00:00 UTC)

```bash
ls -t /var/lib/sn89-hf/*.anchor-v2.json | head -3
f=$(ls -t /var/lib/sn89-hf/*.anchor-v2.json | head -1); python3 -m json.tool $f | grep -E 'anchor"|anchor_ts_ms|agent_n|tick_n'
journalctl -u iq-sn89-hf-anchor --since '-30 min' | grep -E ' v2: | refused|GIVING UP' | tail
```

Pass criteria:
- every window, quiet ones included, logs `v2: … ANCHORED ts=…`;
- `anchor` starts `sn89hf:2:`;
- `anchor_ts_ms` is not null;
- nothing logs `SpaceLimitExceeded` over a full 72-minute epoch (24 anchors of ~100 B
  against 3,100 B);
- `curl -s https://partner.infinitequant.app/sn89/hf/<w>/anchor-v2.json` returns the file.

Third-party check of one window: recompute `root`, `tick_root`, `quote_root` and
`agent_root` from the published files and run `hf.verify_anchor(onchain, …,
quote_root_hex=…, agent_root_hex=…, agent_n=…)` against the CommitmentOf read at
`anchor_block`.

Schedule a re-verification about 12 h after the arm (standing rule). Confirm the deployed
code, then confirm that windows anchored after the arm still verify and that
`agent_n` > 0 once receipts flow.

## 8. Install the units (staged on the branch under `deploy/agent-books/`)

```bash
cd /opt/sn89-signals
install -m 644 deploy/agent-books/sn89-agent-books-{freeze,live}.{service,timer} /etc/systemd/system/
mkdir -p /root/.sn89/agent-books
install -d -o iq -g iq -m 755 /opt/iq-platform/data/live/sn89-agent/weeks
systemctl daemon-reload
systemctl enable --now sn89-agent-books-live.timer          # only once SN89_AGENT_BOOKS_FROM is set
systemctl enable --now sn89-agent-books-freeze.timer
systemctl list-timers 'sn89-agent-books-*'
```

Check the live state, within 5 minutes and from 2026-11-16 onwards:

```bash
systemctl start sn89-agent-books-live.service; journalctl -u sn89-agent-books-live -n 5
python3 -c "import json;d=json.load(open('/opt/iq-platform/data/live/agent-books-live.json'));print(d['as_of_ms'], len(d['books']))"
```

The file must carry `as_of_ms` and `books`, and each book must have `positions`,
`equity`, `balance`, `hwm`, `eliminated_at_ms` and `week_active_buckets`. The intake reads
`eliminated_at_ms` to refuse an eliminated book.

## 9. First freeze (2026-11-23 06:05 UTC) and its proof

```bash
journalctl -u sn89-agent-books-freeze --since today | tail
ls -l /root/.sn89/agent-books/agent-books-week-2026-11-16.json* /opt/iq-platform/data/live/sn89-agent/weeks/
# independent recomputation from a CLEAN cache must match byte for byte:
cd /opt/sn89-signals && .venv/bin/python tools/agent_books_weekly.py --require-anchor \
  --out /tmp/abverify --verify /root/.sn89/agent-books/agent-books-week-2026-11-16.json
curl -s https://partner.infinitequant.app/sn89/agent/weeks/agent-books-week-2026-11-16.json.sha256
```

Pass criteria:
- `MATCH`;
- `entities.<5Hdvy…>.eligible` is true, otherwise the bond is short (step 0);
- the shares sum to 1, or the file has no paid accounts and the share burns;
- `missing_windows` is 0, or is explained.

From 08:00 UTC:
- the validator logs `agentbook week 2026-11-16: N paid account(s)`;
- `~/.sn89/entity-split.jsonl` lines carry the `<5Hdvy…>_<n>` agent-book accounts;
- the Markets payout cycle credits them through `signals_split.agent_payee`. No Markets
  change is needed.

Schedule a 12 h re-verification (standing rule): run `--verify` again from a clean
directory, and check that the vector's `agentbook` share equals the file's shares.

## Rollback

| what | how |
|---|---|
| Stop paying | Add a COMP_WEIGHTS row moving `agentbook`'s share to `reserve` at the next instant, push, restart the validator once. Never edit an old row. |
| Validator ignores agent books | `SN89_AGENT_BOOKS_FROM=0` in `.env.main` (and in the intake env), restart the validator once. |
| Anchor back to v1 | `SN89_AGENT_ANCHOR_V2_FROM=0`, `systemctl restart iq-sn89-hf-anchor`. Windows already anchored v2 stay v2; readers accept both. |
| Timers | `systemctl disable --now sn89-agent-books-{live,freeze}.timer`. |
| Code | `git revert -m 1 <merge>` on master, pytest, push, restart the anchor and the validator. |
