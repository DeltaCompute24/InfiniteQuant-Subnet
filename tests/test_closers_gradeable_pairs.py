"""gradeable_pairs() must be HF board ∪ LF board — not HF board ∪ metadata keys.

Regression for the 2026-08-25 finding: the LF half read `load_bands().keys()`,
the top level of the board DOCUMENT, so it contributed five metadata strings
and no pairs at all. The votable Closers set silently collapsed to
HF_BOARD_V1 and every LF-only pair (TAOUSD, XAGUSD, AUDUSD, USDCAD, HYPEUSD)
was unvotable for as long as it had been listed.

Nothing caught it because the failure is additive and plausible-looking: the
feed kept publishing, on a smaller pair set, with no error and no count in any
log line. So these assert the SHAPE of the disagreement, not just the answer.
"""
import importlib.util
import pathlib

import pytest

from sn89_signals import config, hf

_SRC = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "closers_positions_publisher.py"


def _publisher():
    spec = importlib.util.spec_from_file_location("closers_positions_publisher", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The board document's own non-pair keys. If load_bands() is ever indexed one
# level too shallow again, these are what leak in wearing pair-shaped names.
_DOC_META = {"VERSION", "BANDS", "NOTE", "REFRESHED_AT", "VOL_WINDOW_DAYS"}


def test_every_lf_board_pair_is_votable():
    got = _publisher().gradeable_pairs()
    lf = {a.upper() for a in config.allowed_assets()}
    missing = lf - got
    assert not missing, f"LF board pairs absent from the votable set: {sorted(missing)}"


def test_every_hf_board_pair_is_votable():
    got = _publisher().gradeable_pairs()
    missing = {p.upper() for p in hf.HF_BOARD_V1} - got
    assert not missing, f"HF board pairs absent from the votable set: {sorted(missing)}"


def test_board_document_metadata_never_enters_the_votable_set():
    leaked = _publisher().gradeable_pairs() & _DOC_META
    assert not leaked, (
        f"board-document metadata keys leaked in as pairs: {sorted(leaked)} "
        "— gradeable_pairs() is reading load_bands() instead of allowed_assets()"
    )


@pytest.mark.parametrize("pair", ["TAOUSD", "XAGUSD", "AUDUSD", "USDCAD", "HYPEUSD"])
def test_lf_only_pairs_are_votable(pair):
    """The five pairs the bug hid. Each is on the LF board and absent from
    HF_BOARD_V1, so each is votable ONLY if the LF half of the union works."""
    if pair.upper() not in {a.upper() for a in config.allowed_assets()}:
        pytest.skip(f"{pair} is not currently on the LF board")
    assert pair in _publisher().gradeable_pairs()


def test_votable_set_is_strictly_larger_than_the_hf_board():
    """The whole point of the union. Equality means the LF half contributed
    nothing, which is the exact shape of the original defect."""
    got = _publisher().gradeable_pairs()
    hf_only = {p.upper() for p in hf.HF_BOARD_V1}
    assert got > hf_only, (
        "votable set does not exceed HF_BOARD_V1 — the LF board contributed nothing"
    )
