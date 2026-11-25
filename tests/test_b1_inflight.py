"""B1 移植测试：增量账本（崩溃丢在途撤回记录）。

缺陷复现：旧版撤回记录只在批次全部结束后才落盘，进程崩溃/断电时本批
已移动的文件全部没有撤回记录。修复后期望：
  · 散开进行中每 20 条 + 每源文件夹边界增量落盘 inflight；
  · 崩溃残留 inflight 下次启动自愈合成批次入栈（内容去重防重复）；
  · 记录安全转移（GUI 落盘 / pending 暂存）后清账，转移失败保留。
"""

import json
import types

import pytest

import core.scatter as scatter_mod
from core.scatter import execute_scatter
import undo.history as uh
import undo.store as undo_store
from undo.history import UndoHistoryStore
import ui.main_window as mw


def _rec(name):
    return {"src": rf"D:\p\{name}", "dst": rf"D:\{name}", "mode": "move"}


def _fake_app(stack=None):
    msgs = []
    store = UndoHistoryStore(msgs.append)
    if stack is not None:
        store.stack = stack
    fake = types.SimpleNamespace(undo_store=store)
    return fake, msgs


def _patch_paths(monkeypatch, tmp_path):
    hist = tmp_path / "undo_history.json"
    monkeypatch.setattr(uh, "UNDO_HISTORY_FILE", str(hist))
    monkeypatch.setattr(uh, "UNDO_PENDING_FILE", str(tmp_path / "pending.json"))
    inflight = tmp_path / "inflight.json"
    monkeypatch.setattr(uh, "UNDO_INFLIGHT_FILE", str(inflight))
    monkeypatch.setattr(undo_store, "UNDO_INFLIGHT_FILE", str(inflight))
    return hist, inflight


# ---------------------------------------------------------------- 增量落盘

def test_inflight_flushed_during_run(tmp_path, monkeypatch):
    """25 个文件真跑 execute_scatter：运行中满 20 条即已落盘（非只在结尾）。"""
    inflight = tmp_path / "inflight.json"
    monkeypatch.setattr(scatter_mod, "UNDO_INFLIGHT_FILE", str(inflight))
    sub = tmp_path / "p" / "sub"
    sub.mkdir(parents=True)
    for i in range(25):
        (sub / f"f{i:02d}.txt").write_text("x", encoding="utf-8")
    parent = tmp_path / "p"
    undo_records = []
    midrun_counts = []

    def emit_log(msg):
        # 每条 [移动] 日志产生于记录入列之后：此时读 inflight 可观测运行中落盘
        if msg.startswith("[移动]") and inflight.exists():
            try:
                midrun_counts.append(
                    len(json.loads(inflight.read_text(encoding="utf-8"))))
            except Exception:
                pass

    moved, folders = execute_scatter([str(sub)], str(parent), "move", "skip",
                                     None, 0, False, emit_log,
                                     lambda n: None, lambda *a: None,
                                     lambda: False, undo_records)

    assert moved == 25 and folders == 1
    assert len(undo_records) == 25
    assert midrun_counts, "运行中必须已产生增量落盘（旧版此处为空 = 缺陷复现）"
    assert 20 <= max(midrun_counts) < 25, "满 20 条即落盘，不等全部结束"
    assert len(json.loads(inflight.read_text(encoding="utf-8"))) == 25


def test_inflight_multi_source_boundary(tmp_path, monkeypatch):
    """多源文件夹：每个源边界强制落盘（第一个源 3 条 < 20 也必须已落盘）。"""
    inflight = tmp_path / "inflight.json"
    monkeypatch.setattr(scatter_mod, "UNDO_INFLIGHT_FILE", str(inflight))
    parent = tmp_path / "p"
    first_counts = []
    for name in ("s1", "s2"):
        d = parent / name
        d.mkdir(parents=True)
        for i in range(3):
            (d / f"{name}-{i}.txt").write_text("x", encoding="utf-8")
    undo_records = []

    def emit_log(msg):
        if msg.startswith("[完成]") and "s1" in msg and inflight.exists():
            first_counts.append(
                len(json.loads(inflight.read_text(encoding="utf-8"))))

    execute_scatter([str(parent / "s1"), str(parent / "s2")], str(parent),
                    "move", "skip", None, 0, False, emit_log,
                    lambda n: None, lambda *a: None, lambda: False, undo_records)

    assert first_counts == [3], "源文件夹边界必须强制落盘（3 条不足 20）"


# ---------------------------------------------------------------- 崩溃自愈

def test_heal_inflight_recovers_batch(tmp_path, monkeypatch):
    hist, inflight = _patch_paths(monkeypatch, tmp_path)
    recs = [_rec("a.txt"), _rec("b.txt"), _rec("c.txt")]
    inflight.write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")
    fake, msgs = _fake_app()

    mw.App._load_undo_history(fake)

    assert fake.undo_store.stack == [recs], "inflight 残留必须合成批次入栈"
    assert not inflight.exists(), "自愈落盘成功后清账"
    assert any("已恢复为可撤回批次" in m for m in msgs)
    saved = json.loads(hist.read_text(encoding="utf-8"))
    assert saved == [recs], "自愈批次必须同步持久化到主历史"


def test_heal_skips_already_persisted(tmp_path, monkeypatch):
    """inflight 未清但记录已落盘（上次清账失败）：内容去重，不重复入栈。"""
    hist, inflight = _patch_paths(monkeypatch, tmp_path)
    recs = [_rec("a.txt")]
    hist.write_text(json.dumps([recs], ensure_ascii=False), encoding="utf-8")
    inflight.write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")
    fake, msgs = _fake_app()

    mw.App._load_undo_history(fake)

    assert fake.undo_store.stack == [recs], "已落盘的记录不得重复入栈"
    assert not inflight.exists(), "残留清账"
    assert not any("已恢复" in m for m in msgs)


def test_heal_ignores_corrupt_inflight(tmp_path, monkeypatch):
    hist, inflight = _patch_paths(monkeypatch, tmp_path)
    inflight.write_text("{broken", encoding="utf-8")
    fake, msgs = _fake_app()

    mw.App._load_undo_history(fake)

    assert fake.undo_store.stack == []
    assert not inflight.exists()
    assert any("损坏" in m for m in msgs)


# ---------------------------------------------------------------- 安全转移后清账

def test_persist_on_exit_clears_inflight(tmp_path, monkeypatch):
    """退出兜底落盘成功后清 inflight。"""
    hist, inflight = _patch_paths(monkeypatch, tmp_path)
    rec = _rec("x.txt")
    inflight.write_text(json.dumps([rec], ensure_ascii=False), encoding="utf-8")
    fake, msgs = _fake_app()
    fake.worker = types.SimpleNamespace(
        isRunning=lambda: False, simulate=False, undo_records=[rec])

    mw.App._persist_worker_batch_on_exit(fake)

    assert json.loads(hist.read_text(encoding="utf-8")) == [[rec]]
    assert not inflight.exists()


def _run_silent(tmp_path, monkeypatch, lock_result):
    """受控替身跑 run_silent_folder（无 GUI），预置 inflight，返回其路径。"""
    import main

    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
    monkeypatch.setattr(main, "_scatter_group_id", lambda parent: ("g1", False))
    monkeypatch.setattr(main, "_new_backup_batch", lambda: "batch-test")
    monkeypatch.setattr(main, "_is_protected_system_dir", lambda p: False)
    monkeypatch.setattr(main, "_acquire_scatter_lock", lambda wait_ms=0: lock_result)
    monkeypatch.setattr(main, "_release_scatter_lock", lambda: None)
    monkeypatch.setattr(undo_store, "UNDO_PENDING_FILE",
                        str(tmp_path / "pending.json"))
    inflight = tmp_path / "inflight.json"
    monkeypatch.setattr(undo_store, "UNDO_INFLIGHT_FILE", str(inflight))

    rec = {"src": str(tmp_path / "p" / "f.txt"),
           "dst": str(tmp_path / "f.txt"), "mode": "move"}
    inflight.write_text(json.dumps([rec], ensure_ascii=False), encoding="utf-8")

    def fake_scatter(selected, parent_dir, mode, conflict, suffixes,
                     max_size_mb, simulate, emit_log, countfn, skipfn,
                     stop_check, undo_records, **kw):
        undo_records.append(rec)
        return 1, len(selected)

    monkeypatch.setattr(main, "execute_scatter", fake_scatter)

    sub = tmp_path / "p" / "sub"
    sub.mkdir(parents=True)
    (sub / "f.txt").write_text("x", encoding="utf-8")
    (tmp_path / "error.log").write_text("", encoding="utf-8")

    main.run_silent_folder(str(sub), "one")
    return inflight


def test_silent_clears_inflight_after_pending_written(tmp_path, monkeypatch):
    """pending 暂存成功（记录已安全转移）→ 清 inflight。"""
    inflight = _run_silent(tmp_path, monkeypatch, (True, ""))
    assert not inflight.exists()


def test_silent_keeps_inflight_when_transfer_failed(tmp_path, monkeypatch):
    """无 GUI 且 pending 也写不进去（锁失败）→ 保留 inflight 兜底。"""
    inflight = _run_silent(tmp_path, monkeypatch, (False, "测试占用"))
    assert inflight.exists(), "转移失败必须保留 inflight（下次启动自愈）"
