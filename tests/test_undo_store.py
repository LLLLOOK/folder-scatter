"""UndoHistoryStore 单测（第二轮拆分第 1 步新增）。

Store 是撤回栈的唯一拥有者（undo/history.py，纯 Python 无 Qt）。
覆盖：压栈并组/忙碌延后/退出冲刷/截断淘汰/落盘/备份失效出栈/
启动载入结构归一/退出兜底落栈。数据文件一律重定向到 tmp_path，
不碰程序目录真账本。
"""

import json
import types

import pytest

import undo.history as uh
import undo.store as undo_store
from undo.history import UndoHistoryStore


def _rec(name):
    return {"src": rf"D:\p\{name}", "dst": rf"D:\{name}", "mode": "move"}


def _make_store(msgs=None, enabled=None):
    msgs = msgs if msgs is not None else []
    enabled = enabled if enabled is not None else []
    store = UndoHistoryStore(msgs.append, enabled.append)
    return store, msgs, enabled


def _patch_paths(monkeypatch, tmp_path):
    hist = tmp_path / "undo_history.json"
    monkeypatch.setattr(uh, "UNDO_HISTORY_FILE", str(hist))
    monkeypatch.setattr(uh, "UNDO_PENDING_FILE", str(tmp_path / "pending.json"))
    monkeypatch.setattr(uh, "UNDO_INFLIGHT_FILE", str(tmp_path / "inflight.json"))
    return hist


# ---------------------------------------------------------------- 压栈并组

def test_push_batch_new_group_appends(tmp_path, monkeypatch):
    hist = _patch_paths(monkeypatch, tmp_path)
    store, _, enabled = _make_store()

    assert store.push_batch([_rec("a.txt")], "g1", busy=False) is True
    assert store.push_batch([_rec("b.txt")], "g2", busy=False) is True

    assert len(store.stack) == 2, "不同组不得合并"
    assert store.last_group == "g2"
    assert enabled == [True, True], "压栈成功必须点亮撤回按钮"
    assert json.loads(hist.read_text(encoding="utf-8")) == store.stack


def test_push_batch_same_group_merges_into_top(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    store, _, _ = _make_store()

    store.push_batch([_rec("a.txt")], "g1", busy=False)
    store.push_batch([_rec("b.txt")], "g1", busy=False)

    assert len(store.stack) == 1, "同组续批必须合并进栈顶（一次右键多选=一批）"
    assert store.stack[0] == [_rec("a.txt"), _rec("b.txt")]


def test_push_batch_busy_defers_without_save(tmp_path, monkeypatch):
    hist = _patch_paths(monkeypatch, tmp_path)
    store, _, enabled = _make_store()

    assert store.push_batch([_rec("a.txt")], "g1", busy=True) is False

    assert store.stack == []
    assert store.deferred == [([_rec("a.txt")], "g1", ())]
    assert not hist.exists(), "延后批次未落盘（A4：不回 ack，静默端降级 pending）"
    assert enabled == [], "忙碌延后不得点亮撤回按钮"


def test_push_batch_busy_dedup_by_batch_id(tmp_path, monkeypatch):
    """F28：静默端没等到 ack 换连接重发同批，延后队列按 batch_id 幂等去重；
    重复投递返回 False 不回 ack（首份未落盘，pending 兜底不丢记录）。"""
    _patch_paths(monkeypatch, tmp_path)
    store, _, _ = _make_store()

    assert store.push_batch([_rec("a.txt")], "g1", busy=True,
                            batch_ids=("bid1",)) is False
    assert store.push_batch([_rec("a.txt")], "g1", busy=True,
                            batch_ids=("bid1",)) is False  # 重发同批
    assert store.push_batch([_rec("b.txt")], "g1", busy=True,
                            batch_ids=("bid2",)) is False  # 不同批
    assert store.push_batch([_rec("c.txt")], None, busy=True) is False  # 无 id 批次

    assert len(store.deferred) == 3, "同 bid 重发不得重复入延后队列"
    store.flush_deferred()
    assert store.stack == [[_rec("a.txt"), _rec("b.txt")], [_rec("c.txt")]], \
        "去重后同组批正常并栈、无组批独立成批，不得有重复记录"


def test_flush_deferred_merges_and_saves(tmp_path, monkeypatch):
    hist = _patch_paths(monkeypatch, tmp_path)
    store, _, _ = _make_store()
    store.push_batch([_rec("a.txt")], "g1", busy=False)
    store.push_batch([_rec("b.txt")], "g1", busy=True)   # 忙碌延后
    store.push_batch([_rec("c.txt")], None, busy=True)   # 无组独立批

    store.flush_deferred()

    assert store.deferred == []
    assert store.stack == [[_rec("a.txt"), _rec("b.txt")], [_rec("c.txt")]], \
        "同组延后批并入栈顶，无组延后批独立成批"
    assert json.loads(hist.read_text(encoding="utf-8")) == store.stack


# ---------------------------------------------------------------- 截断淘汰

def test_save_truncates_to_max_batches(tmp_path, monkeypatch):
    hist = _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(uh, "UNDO_HISTORY_MAX_BATCHES", 2)
    store, _, _ = _make_store()
    store.stack = [[_rec("1.txt")], [_rec("2.txt")], [_rec("3.txt")]]

    assert store.save() is True

    assert store.stack == [[_rec("2.txt")], [_rec("3.txt")]], "丢最旧留最新"
    assert json.loads(hist.read_text(encoding="utf-8")) == store.stack


def test_save_failure_keeps_stack(tmp_path, monkeypatch):
    hist = _patch_paths(monkeypatch, tmp_path)
    hist.mkdir()  # 同名目录挡道 → os.replace 必失败
    store, msgs, _ = _make_store()
    store.stack = [[_rec("a.txt")]]

    assert store.save() is False
    assert store.stack == [[_rec("a.txt")]], "落盘失败内存栈必须保留（下次重试）"
    assert any("保存失败" in m for m in msgs)


# ---------------------------------------------------------------- 备份失效出栈

def test_invalidate_backups_drops_referencing_batches(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    root = tmp_path / "散开备份"
    bak = root / "batch-1" / "old.txt"
    store, _, enabled = _make_store()
    store.stack = [
        [_rec("pure-move.txt")],                                  # 纯移动批：不受影响
        [dict(_rec("a.txt"), backup=str(bak))],                   # 引用该根：整批出栈
        [dict(_rec("b.txt"), backup=str(tmp_path / "elsewhere"))],  # 别处备份：保留
    ]

    dropped = store.invalidate_backups(str(root))

    assert dropped == 1
    assert store.stack == [[_rec("pure-move.txt")],
                           [dict(_rec("b.txt"), backup=str(tmp_path / "elsewhere"))]]
    assert store.last_group is None
    assert enabled == [True], "栈结构变化后按余量重设按钮态"


def test_invalidate_backups_no_hit_is_noop(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    store, _, enabled = _make_store()
    store.stack = [[_rec("a.txt")]]

    assert store.invalidate_backups(str(tmp_path / "不存在")) == 0
    assert store.stack == [[_rec("a.txt")]]
    assert enabled == [], "无出栈不得触碰按钮态"


# ---------------------------------------------------------------- 退出兜底

def test_persist_worker_batch_appends_records(tmp_path, monkeypatch):
    hist = _patch_paths(monkeypatch, tmp_path)
    store, _, _ = _make_store()
    worker = types.SimpleNamespace(
        isRunning=lambda: False, simulate=False, undo_records=[_rec("x.txt")])

    store.persist_worker_batch(worker)

    assert store.stack == [[_rec("x.txt")]]
    assert store.last_group is None
    assert json.loads(hist.read_text(encoding="utf-8")) == store.stack


def test_persist_worker_batch_skips_simulate_and_empty(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    store, _, _ = _make_store()
    base = dict(isRunning=lambda: False, undo_records=[_rec("x.txt")])

    store.persist_worker_batch(types.SimpleNamespace(simulate=True, **base))
    store.persist_worker_batch(None)
    store.persist_worker_batch(types.SimpleNamespace(
        isRunning=lambda: True, simulate=False, undo_records=[_rec("x.txt")]))

    assert store.stack == [], "模拟/空记录/运行中/无 worker 均不得落栈"


# ---------------------------------------------------------------- 启动载入结构归一

def test_load_normalizes_group_dict_batches(tmp_path, monkeypatch):
    hist = _patch_paths(monkeypatch, tmp_path)
    grouped = {"group": "g1", "records": [_rec("a.txt")]}
    plain = [_rec("b.txt")]
    hist.write_text(json.dumps([grouped, plain], ensure_ascii=False),
                    encoding="utf-8")
    store, msgs, enabled = _make_store()

    store.load_on_startup()

    assert store.stack == [[_rec("a.txt")], [_rec("b.txt")]], \
        "字典分组批次解包取记录，纯列表批次原样保留"
    assert store.last_group is None, "跨会话不续组"
    assert enabled == [True]
    assert any("已载入" in m for m in msgs)
