"""attest_referrer_succession drain: a transient chain failure holds the queue
and retries; a non-transient one is refused with its reason."""
import importlib.util
import json
import os
import types

_P = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "tools", "attest_referrer_succession.py")
spec = importlib.util.spec_from_file_location("attest", _P)
attest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(attest)


class Resp:
    def __init__(self, ok, msg=""):
        self.success, self.message = ok, msg


def _run(tmp_path, monkeypatch, msg):
    q = tmp_path / "q.jsonl"
    q.write_text(json.dumps({"from": "A", "to": "B"}) + "\n"
                 + json.dumps({"from": "C", "to": "D"}) + "\n")
    monkeypatch.setattr(attest, "successor_live", lambda ch, db, to: True)
    monkeypatch.setattr(attest, "journaled_block", lambda db, f, t: None)
    calls = []
    ch = types.SimpleNamespace(current_block=lambda: 100,
                               commit_referrer_succession=lambda w, f, t: (calls.append(f), Resp(False, msg))[1])
    args = types.SimpleNamespace(queue=str(q), db="x")
    attest.drain(args, ch, object())
    return json.load(open(str(q) + ".state.json")), calls


def test_space_limit_is_retried_and_holds_queue(tmp_path, monkeypatch):
    st, calls = _run(tmp_path, monkeypatch, "Module error: SpaceLimitExceeded")
    assert st["A:B"]["status"] == "retry" and st["A:B"]["attempts"] == 1
    assert "C:D" not in st and calls == ["A"]


def test_permanent_failure_is_refused_with_reason(tmp_path, monkeypatch):
    st, calls = _run(tmp_path, monkeypatch, "BadOrigin")
    assert st["A:B"]["status"] == "refused" and "BadOrigin" in st["A:B"]["reason"]
    assert calls == ["A", "C"]


def test_retry_waits_for_next_at(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, "SpaceLimitExceeded")
    st, calls = _run(tmp_path, monkeypatch, "SpaceLimitExceeded")
    assert calls == [] and st["A:B"]["attempts"] == 1
