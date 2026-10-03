from sn89_signals import config


def test_until_closes_the_window_and_keeps_history(monkeypatch):
    monkeypatch.setattr(config, "HF_CUSTOM_BANDS_FROM", 1_000)
    monkeypatch.setattr(config, "HF_CUSTOM_BANDS_UNTIL", 2_000)
    assert config.custom_bands_enforced_as_of(999) is False
    assert config.custom_bands_enforced_as_of(1_000) is True
    assert config.custom_bands_enforced_as_of(1_999.9) is True
    assert config.custom_bands_enforced_as_of(2_000) is False


def test_until_unset_means_no_end(monkeypatch):
    monkeypatch.setattr(config, "HF_CUSTOM_BANDS_FROM", 1_000)
    monkeypatch.setattr(config, "HF_CUSTOM_BANDS_UNTIL", 0)
    assert config.custom_bands_enforced_as_of(10**12) is True


def test_mainnet_default_unchanged():
    assert config.HF_CUSTOM_BANDS_UNTIL == 0 or config.HF_CUSTOM_BANDS_FROM == 0
