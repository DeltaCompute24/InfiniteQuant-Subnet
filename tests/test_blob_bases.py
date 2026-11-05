"""_find_blob reads R2_PUBLIC_BASE, then any R2_EXTRA_BASES, matching on url_tag."""
import importlib.util
import pathlib

from sn89_signals import bucket, chain, config

_p = pathlib.Path(__file__).resolve().parents[1] / "neurons" / "validator.py"
_spec = importlib.util.spec_from_file_location("sn89_validator_mod", _p)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_find_blob = next(v for v in vars(_mod).values()
                  if isinstance(v, type) and "_find_blob" in vars(v))._find_blob

HK, NONCE = "5Abc", "n1"
LOCAL, RELAY = "http://local:1", "https://relay/blob"
BLOB = {"ok": 1}


def _serve(monkeypatch, host):
    def fetch(url, **_):
        if url == f"{host}/{HK}/index.json":
            return {"nonces": [NONCE]}
        if url == f"{host}/{HK}/{NONCE}.json":
            return BLOB
        return None
    monkeypatch.setattr(bucket, "fetch", fetch)


def test_extra_base_finds_a_blob_tagged_to_it(monkeypatch):
    monkeypatch.setattr(config, "R2_PUBLIC_BASE", LOCAL)
    monkeypatch.setattr(config, "R2_EXTRA_BASES", (RELAY,))
    _serve(monkeypatch, RELAY)
    tag = chain.url_tag(f"{RELAY}/{HK}/{NONCE}.json")
    assert _find_blob(None, HK, tag) == BLOB


def test_primary_base_still_wins(monkeypatch):
    monkeypatch.setattr(config, "R2_PUBLIC_BASE", LOCAL)
    monkeypatch.setattr(config, "R2_EXTRA_BASES", (RELAY,))
    _serve(monkeypatch, LOCAL)
    tag = chain.url_tag(f"{LOCAL}/{HK}/{NONCE}.json")
    assert _find_blob(None, HK, tag) == BLOB


def test_no_extra_base_misses_a_blob_on_another_host(monkeypatch):
    monkeypatch.setattr(config, "R2_PUBLIC_BASE", LOCAL)
    monkeypatch.setattr(config, "R2_EXTRA_BASES", ())
    _serve(monkeypatch, RELAY)
    tag = chain.url_tag(f"{RELAY}/{HK}/{NONCE}.json")
    assert _find_blob(None, HK, tag) is None


def test_a_tag_for_another_host_is_not_served(monkeypatch):
    monkeypatch.setattr(config, "R2_PUBLIC_BASE", LOCAL)
    monkeypatch.setattr(config, "R2_EXTRA_BASES", (RELAY,))
    _serve(monkeypatch, RELAY)
    tag = chain.url_tag(f"{LOCAL}/{HK}/{NONCE}.json")
    assert _find_blob(None, HK, tag) is None
