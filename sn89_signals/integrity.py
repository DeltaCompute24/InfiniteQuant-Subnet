"""Miner integrity verdicts, fetched from the owner-hosted service.

WHY THE RULE IS NOT IN THIS REPO
--------------------------------
A published threshold is a target. Every constant that decides whether a miner is paid was
readable in this repo, and in August 2026 a group of hotkeys was observed steering to the
published numbers rather than to the behaviour those numbers were chosen to measure. Once a
gate can be read it stops measuring conduct and starts measuring willingness to satisfy an
equation.

So detection moved to a closed service and only the VERDICT is shipped. The remaining
in-repo gates are unchanged and still enforced; they are a floor, not the whole test. This is the same split Taoshi uses on SN8, where the public PTN repo
carries a client for `plagiarism.ultron.ts.taoshi.io/plagiarism/elimination_scores` and no
detection code at all, and the live endpoint returns flagged miners only with no /docs,
/health or sub-threshold visibility.

REPLAYABILITY IS NOT SACRIFICED — READ THIS BEFORE CALLING IT CENTRALISATION
---------------------------------------------------------------------------
Every verdict carries `time`, and every verdict is appended to a published log. A replayer
applies each one AS-OF its timestamp and reproduces our weight vector exactly. The verdict is
an INPUT, like a price tick: you do not need to be able to re-derive a price from first
principles to replay a backtest against it. What you cannot do is predict a FUTURE verdict —
which is the property being bought.

FAIL CLOSED, NOT OPEN
---------------------
An unreachable service must never read as "nobody is flagged". That would un-gate every
flagged miner the moment the endpoint hiccups, and it would do it silently. The last good
verdict set is cached on disk and reused; only a cold start with no cache yields an empty
set, and that logs at ERROR.
"""
import json
import os
import time
import urllib.request

INTEGRITY_URL = os.getenv(
    "SN89_INTEGRITY_URL",
    "https://partner.infinitequant.app/sn89/integrity/elimination_scores")
INTEGRITY_CACHE = os.getenv("SN89_INTEGRITY_CACHE", "/tmp/sn89-integrity-cache.json")
INTEGRITY_REFRESH_S = int(os.getenv("SN89_INTEGRITY_REFRESH_S", "900"))
# A miner at or above this is excluded from weight. Kept here on purpose: the THRESHOLD is
# useless to a miner who cannot see its own score, and a validator has to be able to see the
# arithmetic that moved the weights.
INTEGRITY_FLAG_AT = float(os.getenv("SN89_INTEGRITY_FLAG_AT", "0.80"))

_cache = {"at": 0.0, "scores": None}


def _read_disk_cache():
    try:
        with open(INTEGRITY_CACHE) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except Exception:                                       # noqa: BLE001
        return None


def integrity_scores(now=None):
    """{hotkey: score}. Cached in memory, then on disk, then empty."""
    now = time.time() if now is None else now
    if _cache["scores"] is not None and (now - _cache["at"]) < INTEGRITY_REFRESH_S:
        return _cache["scores"]
    try:
        # ⚠ THE User-Agent IS LOAD-BEARING. partner.infinitequant.app sits behind
        # Cloudflare, which 403s urllib's default `Python-urllib/3.x` while allowing curl.
        # Measured 2026-08-20 from iq-main: curl 200, urllib default 403, urllib with any
        # real UA 200. A validator that omits it sees a hard 403 forever and — because this
        # module fails closed — quietly runs on stale cache, or on nothing at all.
        req = urllib.request.Request(
            INTEGRITY_URL, headers={"User-Agent": "sn89-validator/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = json.loads(r.read())
        scores = {hk: float(v.get("score", 0.0)) for hk, v in raw.items()
                  if isinstance(v, dict)}
        _cache.update(at=now, scores=scores)
        try:
            tmp = INTEGRITY_CACHE + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(scores, fh)
            os.replace(tmp, INTEGRITY_CACHE)
        except OSError:
            pass
        return scores
    except Exception as exc:                                # noqa: BLE001
        disk = _read_disk_cache()
        if disk is not None:
            _cache.update(at=now, scores=disk)
            print(f"[integrity] fetch failed ({exc}); using cached verdicts "
                  f"({len(disk)} flagged)")
            return disk
        print(f"[integrity] ERROR fetch failed ({exc}) and NO cache — "
              f"no integrity verdicts applied this cycle")
        _cache.update(at=now, scores={})
        return {}


def integrity_ok(hotkey, now=None):
    """False when this hotkey is flagged at or above the threshold."""
    return integrity_scores(now).get(hotkey, 0.0) < INTEGRITY_FLAG_AT
