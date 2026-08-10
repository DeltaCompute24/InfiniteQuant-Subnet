"""hf_scoring_config must work with NO argument.

Regression 2026-08-10: it gained a `time.time()` default while hf.py had no
`import time`, so every bare call raised NameError. The whole test suite passed
because every test — and the validator — passes `now` explicitly. The only
caller that does not is build_hf_scoreboard.py, which then crash-looped for
9 hours behind a stale JSON file that the website kept serving.

The lesson this pins: a default argument path with no test is an untested code
path, and "the suite is green" says nothing about it.
"""
import time

from sn89_signals import config, hf


class TestScoringConfigDefaults:
    def test_bare_call_works(self):
        before = config.MINER_EMISSION_CAP
        with hf_scoring_config_bare():
            pass
        assert config.MINER_EMISSION_CAP == before, "constants must be restored"

    def test_bare_call_uses_now(self):
        with hf.hf_scoring_config():
            bare = config.MINER_EMISSION_CAP
        with hf.hf_scoring_config(time.time()):
            explicit = config.MINER_EMISSION_CAP
        assert bare == explicit

    def test_explicit_instant_uses_that_era(self):
        ERA_60 = 1_785_000_000.0
        with hf.hf_scoring_config(ERA_60):
            assert config.MINER_EMISSION_CAP == 0.60

    def test_restores_on_exception(self):
        before = dict(
            cap=config.MINER_EMISSION_CAP, imm=config.IMMUNITY_S,
            decay=config.EMISSION_DECAY_S)
        try:
            with hf.hf_scoring_config():
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert config.MINER_EMISSION_CAP == before["cap"]
        assert config.IMMUNITY_S == before["imm"]
        assert config.EMISSION_DECAY_S == before["decay"]


def hf_scoring_config_bare():
    """Named helper so the no-argument call site is explicit in the test body."""
    return hf.hf_scoring_config()
