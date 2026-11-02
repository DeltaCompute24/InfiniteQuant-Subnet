"""chain.commit_ok: a refused set_commitment must not read as committed (2026-11-02)."""
from sn89_signals import chain


class _Resp:
    """Shape of bittensor 10's ExtrinsicResponse: __len__ == 2, so always truthy."""

    def __init__(self, success, message=""):
        self.success, self.message = success, message

    def __len__(self):
        return 2


def test_refused_extrinsic_response_is_not_ok():
    r = _Resp(False, "SpaceLimitExceeded")
    assert bool(r) is True                       # the trap
    assert chain.commit_ok(r) == (False, "SpaceLimitExceeded")


def test_accepted_extrinsic_response_is_ok():
    assert chain.commit_ok(_Resp(True, "Success")) == (True, "Success")


def test_tuple_and_bool_shapes():
    assert chain.commit_ok((False, "nope")) == (False, "nope")
    assert chain.commit_ok((True,)) == (True, "")
    assert chain.commit_ok(True) == (True, "")
    assert chain.commit_ok(False) == (False, "")
