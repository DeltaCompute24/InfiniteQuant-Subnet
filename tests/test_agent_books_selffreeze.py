"""An outside validator pays the agent-book share with no extra setup (2026-11-12, Yuma vtrust):
mainnet defaults, the public receipts fallback, and the background self-freeze of a missing week."""
import importlib.util
import os
import types
from pathlib import Path

from sn89_signals import agent_books_io as abio
from sn89_signals import config

ROOT = Path(__file__).resolve().parents[1]
NOW = 1794525000.0          # 2026-11-12 23:50 UTC: pays week 2026-11-02


def _weekly():
    spec = importlib.util.spec_from_file_location("abw", ROOT / "tools" / "agent_books_weekly.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_mainnet_defaults(monkeypatch):
    monkeypatch.delenv("SN89_AGENT_BOOKS_FROM", raising=False)
    monkeypatch.delenv("SN89_AGENT_BOOKS_RECEIPTS_URLS", raising=False)
    import importlib
    c = importlib.reload(config)
    try:
        assert c.AGENT_BOOKS_FROM_UNIX == 1793766600
        assert c.AGENT_BOOKS_RECEIPTS_URLS == "https://partner.infinitequant.app/sn89/agent/books/index.json"
        assert c.agent_books_active(NOW)
    finally:
        importlib.reload(config)


def test_receipt_urls_fallback_only_without_local_dir(tmp_path, monkeypatch):
    w = _weekly()
    monkeypatch.setattr(config, "AGENT_BOOKS_RECEIPTS_URLS", "https://x/index.json")
    a = types.SimpleNamespace(receipts_dir=str(tmp_path), receipts_url=None)
    assert w.receipt_urls(a) == []                       # intake host: local files only
    a = types.SimpleNamespace(receipts_dir="", receipts_url=None)
    assert w.receipt_urls(a) == ["https://x/index.json"]
    a = types.SimpleNamespace(receipts_dir=str(tmp_path / "missing"), receipts_url=None)
    assert w.receipt_urls(a) == ["https://x/index.json"]
    a = types.SimpleNamespace(receipts_dir=str(tmp_path), receipts_url=["https://y"])
    assert w.receipt_urls(a) == ["https://y"]


def _active(monkeypatch):
    monkeypatch.setattr(config, "AGENT_BOOKS_FROM_UNIX", 1793766600)


def test_self_freeze_starts_once_for_a_missing_week(tmp_path, monkeypatch):
    _active(monkeypatch)
    calls = []
    out = str(tmp_path / "books")
    r = abio.ensure_week_async(NOW, out_dir=out, receipts_dir="", spawn=calls.append)
    assert r == "started" and len(calls) == 1
    argv = calls[0]
    assert argv[argv.index("--week") + 1] == "2026-11-02"
    assert argv[argv.index("--out") + 1] == out
    assert argv[argv.index("--receipts-dir") + 1] == ""
    assert argv[1].endswith("tools/agent_books_weekly.py") and os.path.isfile(argv[1])
    # running: not started again within the hour, started again after it
    assert abio.ensure_week_async(NOW + 60, out_dir=out, receipts_dir="", spawn=calls.append) == "running"
    assert abio.ensure_week_async(NOW + 3700, out_dir=out, receipts_dir="", spawn=calls.append) == "started"
    assert len(calls) == 2


def test_self_freeze_skips_when_frozen_or_local(tmp_path, monkeypatch):
    _active(monkeypatch)
    calls = []
    out = tmp_path / "books"
    out.mkdir()
    (out / "agent-books-week-2026-11-02.json").write_text("{}")
    assert abio.ensure_week_async(NOW, out_dir=str(out), receipts_dir="", spawn=calls.append) == "frozen"
    assert abio.ensure_week_async(NOW, out_dir=str(tmp_path / "b2"), receipts_dir=str(tmp_path),
                                  spawn=calls.append) == "local_receipts"
    assert calls == []


def test_self_freeze_inactive(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_BOOKS_FROM_UNIX", 0)
    assert abio.ensure_week_async(NOW, out_dir=str(tmp_path), receipts_dir="", spawn=list.append) == "inactive"
