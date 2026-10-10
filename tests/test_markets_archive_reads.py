from sn89_signals import markets


class _St:
    def __init__(self, name): self.name = name


class _C:
    def __init__(self, head):
        self.st, self._a, self.head = _St("public"), _St("archive"), head
    def _archive(self): return self._a
    def current_block(self): return self.head


def _view(head):
    v = markets.RpcChainView.__new__(markets.RpcChainView)
    v.c, v.netuid = _C(head), 89
    return v


def test_historic_reads_go_to_the_archive_only(monkeypatch):
    monkeypatch.setattr(markets.config, "NETWORK", "finney", raising=False)
    v = _view(10_000)
    assert [s.name for s in v._nodes(9_000)] == ["archive"]


def test_recent_reads_try_public_then_archive(monkeypatch):
    monkeypatch.setattr(markets.config, "NETWORK", "finney", raising=False)
    v = _view(10_000)
    assert [s.name for s in v._nodes(9_950)] == ["public", "archive"]


def test_testnet_reads_its_own_node(monkeypatch):
    monkeypatch.setattr(markets.config, "NETWORK", "test", raising=False)
    v = _view(10_000)
    assert [s.name for s in v._nodes(1)] == ["public"]
