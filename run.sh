#!/usr/bin/env bash
# SN89 neuron launcher with auto-update.
#
#   bash run.sh validator --wallet.name <w> --wallet.hotkey <vali>
#   bash run.sh miner follow --wallet.name <w> --wallet.hotkey <miner>
#   bash run.sh miner submit --pair BTCUSD --direction LONG --wallet.name <w> --wallet.hotkey <miner>
#
# Run from the repo root (where .venv lives). On a timer it checks origin/<branch>;
# when a new version is published it pulls, reinstalls changed deps, and restarts
# the neuron — so validators stay on consensus-compatible grading code and miners
# on the current submission format. It also restarts the neuron if it crashes.
#
# Env knobs:
#   SN89_AUTO_UPDATE=0        disable updates (just run + auto-restart on crash)
#   SN89_UPDATE_INTERVAL_S=N  seconds between update checks (default 1800 = 30 min)
#   SN89_UPDATE_BRANCH=name   branch to track (default master)
set -uo pipefail

ROLE="${1:-}"; [ "$#" -gt 0 ] && shift || true
case "$ROLE" in
  validator|miner) : ;;
  *) echo "usage: bash run.sh {validator|miner} [neuron args...]"; exit 1 ;;
esac

PY=.venv/bin/python
PIP=.venv/bin/pip
[ -x "$PY" ] || { echo "run.sh: $PY not found — create the venv + install deps first (see README)"; exit 1; }

BRANCH="${SN89_UPDATE_BRANCH:-master}"
INTERVAL="${SN89_UPDATE_INTERVAL_S:-1800}"
AUTO="${SN89_AUTO_UPDATE:-1}"
SCRIPT="neurons/${ROLE}.py"

log() { printf '%s [run.sh] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# Pull origin/$BRANCH if behind. Returns 0 ONLY when it actually updated.
update_if_behind() {
  [ "$AUTO" = "1" ] || return 1
  git fetch origin "$BRANCH" --quiet 2>/dev/null || { log "git fetch failed (offline?) — keeping current code"; return 1; }
  local cur new; cur=$(git rev-parse HEAD 2>/dev/null); new=$(git rev-parse "origin/$BRANCH" 2>/dev/null)
  [ -n "$new" ] && [ "$cur" != "$new" ] || return 1
  log "new version available (${cur:0:7} -> ${new:0:7}) — updating"
  local req_before; req_before=$(sha1sum requirements.txt 2>/dev/null || true)
  if ! git pull --ff-only origin "$BRANCH" --quiet; then
    log "fast-forward pull failed (local edits in the way?) — staying on current version"
    return 1
  fi
  if [ "$req_before" != "$(sha1sum requirements.txt 2>/dev/null || true)" ]; then
    log "requirements.txt changed — reinstalling deps"
    "$PIP" install -q -r requirements.txt || log "pip install reported errors (continuing)"
  fi
  log "updated to $(git rev-parse --short HEAD)"
}

PID=""
cleanup() { [ -n "$PID" ] && kill "$PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

while true; do
  update_if_behind || true
  log "starting $ROLE — $PY $SCRIPT $*"
  "$PY" "$SCRIPT" "$@" &
  PID=$!

  restarted_for_update=0
  last=$SECONDS
  # supervise: poll liveness fast (crash detection); check for updates every INTERVAL
  while kill -0 "$PID" 2>/dev/null; do
    sleep 10
    if [ "$((SECONDS - last))" -ge "$INTERVAL" ]; then
      last=$SECONDS
      if update_if_behind; then
        log "restarting $ROLE on the new version"
        kill "$PID" 2>/dev/null; wait "$PID" 2>/dev/null || true
        restarted_for_update=1
        break
      fi
    fi
  done

  if [ "$restarted_for_update" = "0" ]; then
    wait "$PID" 2>/dev/null; code=$?
    log "$ROLE exited (code ${code}) — restarting in 10s"
    sleep 10
  fi
done
