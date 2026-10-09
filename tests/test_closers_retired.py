from sn89_signals import config


def test_closers_has_no_share_after_mainnet_retirement():
    if config.COMP_WEIGHTS_HISTORY[0][0] == 0 and len(config.COMP_WEIGHTS_HISTORY) == 1:
        return  # env override (testnet) — not the mainnet schedule
    assert config.comp_weights_as_of(1791504000).get("closers", 0.0) == 0.0
    assert config.comp_weights_as_of(1791503999).get("closers", 0.0) > 0.0
