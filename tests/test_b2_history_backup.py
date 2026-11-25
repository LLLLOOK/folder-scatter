"""B2 移植测试：撤回账本 .bak/.corrupt 双保险。

缺陷复现：undo_history.json 损坏（断电写一半等）时，旧版静默置空——
所有可撤回批次无声消失；pending 合并还会以空底覆盖写回。
修复后期望：写前留 .bak；主文件损坏从 .bak 恢复并提示；主备双坏隔离为
.corrupt-<时间戳> 保留现场 + 醒目告警，绝不静默置空。
把修复回退（删掉 .bak/.corrupt 逻辑）时本文件用例应变红。
"""

import json
import types

import pytest

import undo.history as uh
from undo.history import UndoHistoryStore
import ui.main_window as mw


def _fake_app(stack=None):
    msgs = []
    store = UndoHistoryStore(msgs.append)
    if stack is not None:
        store.stack = stack
    fake = types.SimpleNamespace(undo_store=store)
    return fake, msgs


def _batch(name):
    return [{"src": rf"D:\p\{name}", "dst": rf"D:\{name}", "mode": "move"}]


# ---------------------------------------------------------------- 写侧 .bak

def test_save_creates_bak_of_previous_content(tmp_path, monkeypatch):
    hist = tmp_path / "undo_history.json"
    old = [_batch("old.txt")]
    hist.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(uh, "UNDO_HISTORY_FILE", str(hist))
    fake, _ = _fake_app(stack=[_batch("new.txt")])

    mw.App._save_undo_history(fake)

    assert json.loads(hist.read_text(encoding="utf-8")) == fake.undo_store.stack
    bak = tmp_path / "undo_history.json.bak"
    assert bak.exists(), "覆盖写前必须把旧账本留为 .bak"
    assert json.loads(bak.read_text(encoding="utf-8")) == old


# ---------------------------------------------------------------- 读侧恢复

def test_corrupt_main_recovers_from_bak(tmp_path, monkeypatch):
    hist = tmp_path / "undo_history.json"
    good = [_batch("a.txt")]
    (tmp_path / "undo_history.json.bak").write_text(
        json.dumps(good, ensure_ascii=False), encoding="utf-8")
    hist.write_text('[{"src": "半截 json', encoding="utf-8")  # 断电写一半
    monkeypatch.setattr(uh, "UNDO_HISTORY_FILE", str(hist))
    monkeypatch.setattr(uh, "UNDO_PENDING_FILE", str(tmp_path / "none.json"))
    fake, msgs = _fake_app()

    mw.App._load_undo_history(fake)

    assert fake.undo_store.stack == good, "主文件损坏应从 .bak 恢复，不得静默置空"
    assert any(".bak" in m and "恢复" in m for m in msgs)


def test_double_corrupt_quarantines_and_warns(tmp_path, monkeypatch):
    hist = tmp_path / "undo_history.json"
    hist.write_text("{{{{", encoding="utf-8")
    (tmp_path / "undo_history.json.bak").write_text("]]]]", encoding="utf-8")
    monkeypatch.setattr(uh, "UNDO_HISTORY_FILE", str(hist))
    monkeypatch.setattr(uh, "UNDO_PENDING_FILE", str(tmp_path / "none.json"))
    fake, msgs = _fake_app()

    mw.App._load_undo_history(fake)

    assert fake.undo_store.stack == []
    assert not hist.exists(), "双坏主文件应改名隔离，不得留在原位被当空历史"
    corrupt = list(tmp_path.glob("undo_history.json.corrupt-*"))
    assert len(corrupt) == 1, "现场必须保留为 .corrupt-<时间戳>"
    assert corrupt[0].read_text(encoding="utf-8") == "{{{{"
    assert any("均已损坏" in m for m in msgs), "双坏必须醒目告警，不得静默"


def test_corrupt_main_without_bak_also_quarantines(tmp_path, monkeypatch):
    hist = tmp_path / "undo_history.json"
    hist.write_text("not json at all", encoding="utf-8")
    monkeypatch.setattr(uh, "UNDO_HISTORY_FILE", str(hist))
    monkeypatch.setattr(uh, "UNDO_PENDING_FILE", str(tmp_path / "none.json"))
    fake, msgs = _fake_app()

    mw.App._load_undo_history(fake)

    assert fake.undo_store.stack == []
    assert len(list(tmp_path.glob("undo_history.json.corrupt-*"))) == 1
    assert any("均已损坏" in m for m in msgs)


# ---------------------------------------------------------------- pending 合并段

def test_pending_merge_recovers_base_from_bak(tmp_path, monkeypatch):
    """合并 pending 时主文件损坏：旧版静默 base=[] 再覆盖写回，旧历史全丢；
    修复后从 .bak 恢复旧底再合并。"""
    hist = tmp_path / "undo_history.json"
    pend = tmp_path / "undo_history_pending.json"
    old = [_batch("old.txt")]
    new = [_batch("new.txt")]
    hist.write_text("{broken", encoding="utf-8")
    (tmp_path / "undo_history.json.bak").write_text(
        json.dumps(old, ensure_ascii=False), encoding="utf-8")
    pend.write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(uh, "UNDO_HISTORY_FILE", str(hist))
    monkeypatch.setattr(uh, "UNDO_PENDING_FILE", str(pend))
    fake, msgs = _fake_app()

    mw.App._load_undo_history(fake)

    merged = json.loads(hist.read_text(encoding="utf-8"))
    assert merged == old + new, "合并底必须来自 .bak 恢复的旧历史"
    assert not pend.exists()
    assert fake.undo_store.stack == old + new
    assert any(".bak" in m and "恢复" in m for m in msgs)
