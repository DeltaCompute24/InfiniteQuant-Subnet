#!/usr/bin/env bash
# Publish the authoritative validator's journal as a verifiable checkpoint, so any
# miner/skeptic can run audit_journal.py and confirm the on-chain weights match the
# journal (docs/single-validator-model.md). Read-only on the validator DB.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="${SN89_REPO:-/opt/sn89-signals}"
OUT="${SN89_CHECKPOINT_OUT:-/var/www/sn89/checkpoint.json}"
TMP="${OUT}.tmp"

set -a; [ -f "$REPO/.env.test" ] && . "$REPO/.env.test"; set +a
cd "$REPO"

# write to a temp file then atomically rename, so a reader never sees a partial file
"$REPO/.venv/bin/python" scripts/export_checkpoint.py "$TMP" --chain
mv -f "$TMP" "$OUT"
echo "published $OUT"
