"""A2 移植测试：undo_history.json 主写路径 / pending 合并 / 静默暂存写入持锁。

缺陷复现：另一进程（用独立线程持同一命名互斥体模拟）持锁期间，旧版三条
写路径仍无锁执行，并发写 last-writer-wins 互覆丢批次。
修复后期望：抢不到锁一律不写（绝不无锁写），告警/错误日志留痕，锁释放后
下次操作正常落盘。把修复回退（去掉锁判断直接写）时本文件用例应变红。
"""

import contextlib
import ctypes
import json
import threading
import types

import pytest

from core.locking import _MUTEX_NAME, _history_lock
import undo.history as uh
import undo.store as undo_store
from undo.history import UndoHistoryStore
import ui.main_window as mw


@contextlib.contextmanager
def mutex_held_by_other_thread():
    """另一线程持有命名互斥体，模拟另一进程持锁（同线程持锁会被 Windows
    互斥体递归语义放行，无法模拟跨进程互斥，故必须独立线程）。"""
    acquired = threading.Event()
    done = threading.Event()
    errors = []

    def _holder():
        k = ctypes.windll.kernel32
        k.CreateMutexW.restype = ctypes.c_void_p
        k.WaitForSingleObject.restype = ctypes.c_ulong
        h = k.CreateMutexW(None, False, _MUTEX_NAME)
        if not h:
            errors.append("CreateMutexW failed")
            acquired.set()
            return
        wr = k.WaitForSingleObject(h, 5000)
        if wr not in (0x0, 0x80):  # WAIT_OBJECT_0 / WAIT_ABANDONED
            errors.append(f"WaitForSingleObject={wr}")
            acquired.set()
            k.CloseHandle(ctypes.c_void_p(h))
            return
        acquired.set()
        done.wait(10)
        k.ReleaseMutex(ctypes.c_void_p(h))
        k.CloseHandle(ctypes.c_void_p(h))

    t = threading.Thread(target=_holder, daemon=True)
    t.start()
    assert acquired.wait(5), "持锁线程未启动"
    assert not errors, errors
    try:
        yield
    finally:
        done.set()
        t.join(5)


# ---------------------------------------------------------------- 锁原语本身

def test_history_lock_mutual_exclusion():
    # 无人持锁时可获取
    with _history_lock(500) as (ok, reason):
        assert ok, reason
    # 另一线程持锁时获取失败；释放后恢复可获取
    with mutex_held_by_other_thread():
        with _history_lock(200) as (ok, reason):
            assert not ok
            assert reason
    with _history_lock(2000) as (ok, reason):
        assert ok, reason


# ---------------------------------------------------------------- GUI 主写路径

def _fake_app():
    msgs = []
    store = UndoHistoryStore(msgs.append)
    store.stack = [[{"src": r"D:\p\a.txt", "dst": r"D:\a.txt", "mode": "move"}]]
    fake = types.SimpleNamespace(undo_store=store)
    return fake, msgs


def test_save_undo_history_skips_when_lock_busy(tmp_path, monkeypatch):
    hist = tmp_path / "undo_history.json"
    monkeypatch.setattr(uh, "UNDO_HISTORY_FILE", str(hist))
    fake, msgs = _fake_app()

    with mutex_held_by_other_thread():
        mw.App._save_undo_history(fake)
        assert not hist.exists(), "抢不到锁时不得无锁写 undo_history.json"
        assert any("暂缓" in m for m in msgs), "须有暂缓告警留痕"

    mw.App._save_undo_history(fake)
    assert hist.exists(), "锁释放后应正常落盘"
    assert json.loads(hist.read_text(encoding="utf-8")) == fake.undo_store.stack


# ---------------------------------------------------------------- 启动合并 pending

def _fake_app_for_load():
    msgs = []
    fake = types.SimpleNamespace(undo_store=UndoHistoryStore(msgs.append))
    return fake, msgs


def test_load_merge_pending_skips_when_lock_busy(tmp_path, monkeypatch):
    hist = tmp_path / "undo_history.json"
    pend = tmp_path / "undo_history_pending.json"
    batch = [{"src": r"D:\p\b.txt", "dst": r"D:\b.txt", "mode": "move"}]
    pend.write_text(json.dumps([batch], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(uh, "UNDO_HISTORY_FILE", str(hist))
    monkeypatch.setattr(uh, "UNDO_PENDING_FILE", str(pend))
    fake, msgs = _fake_app_for_load()

    with mutex_held_by_other_thread():
        mw.App._load_undo_history(fake)
        assert pend.exists(), "抢不到锁时不得合并/删除 pending（保留待重试）"
        assert not hist.exists(), "抢不到锁时不得无锁写主历史"
        assert fake.undo_store.stack == []
        assert any("暂缓" in m for m in msgs)

    mw.App._load_undo_history(fake)
    assert not pend.exists(), "锁释放后合并成功才删 pending"
    assert fake.undo_store.stack == [batch]


# ---------------------------------------------------------------- 静默 pending 写入（main.py 分支）

@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _run_silent(tmp_path, monkeypatch, lock_result):
    """以受控替身跑 run_silent_folder：散开执行/分组/备份批号为替身，
    日志重定向到 tmp_path，锁结果由参数决定。返回 (append调用次数, errlog)。"""
    import main

    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
    monkeypatch.setattr(main, "_scatter_group_id", lambda parent: ("g1", False))
    monkeypatch.setattr(main, "_new_backup_batch", lambda: "batch-test")
    # tmp 目录命中系统保护目录前缀；本用例不验证该防线，放行
    monkeypatch.setattr(main, "_is_protected_system_dir", lambda p: False)
    monkeypatch.setattr(main, "_acquire_scatter_lock", lambda wait_ms=0: lock_result)
    monkeypatch.setattr(main, "_release_scatter_lock", lambda: None)

    rec = {"src": str(tmp_path / "p" / "f.txt"),
           "dst": str(tmp_path / "f.txt"), "mode": "move"}

    def fake_scatter(selected, parent_dir, mode, conflict, suffixes,
                     max_size_mb, simulate, emit_log, countfn, skipfn,
                     stop_check, undo_records, **kw):
        undo_records.append(rec)
        return 1, len(selected)

    monkeypatch.setattr(main, "execute_scatter", fake_scatter)

    calls = []
    real_append = undo_store._append_undo_pending

    def spy_append(records, group=None):
        calls.append((records, group))
        return real_append(records, group)

    monkeypatch.setattr(main, "_append_undo_pending", spy_append)

    sub = tmp_path / "p" / "sub"
    sub.mkdir(parents=True)
    (sub / "f.txt").write_text("x", encoding="utf-8")
    # _append_utf8_bom 对不存在的新文件会吞错不写（旧版既有行为，与本修复无关），
    # 预建空文件让错误日志可追加
    (tmp_path / "error.log").write_text("", encoding="utf-8")

    main.run_silent_folder(str(sub), "one")
    return calls, tmp_path / "error.log", rec


def test_silent_pending_not_written_when_lock_busy(tmp_path, monkeypatch, qapp):
    calls, errlog, rec = _run_silent(tmp_path, monkeypatch, (False, "测试占用"))
    assert calls == [], "锁失败时绝不无锁写 pending（修复前此处会被调用）"
    text = errlog.read_text(encoding="utf-8-sig")
    assert "撤回暂存未能写入" in text, "锁失败须写 error.log 留痕"


def test_silent_pending_written_when_lock_ok(tmp_path, monkeypatch, qapp):
    pend = tmp_path / "pending.json"
    monkeypatch.setattr(undo_store, "UNDO_PENDING_FILE", str(pend))
    calls, errlog, rec = _run_silent(tmp_path, monkeypatch, (True, ""))
    assert len(calls) == 1
    data = json.loads(pend.read_text(encoding="utf-8"))
    assert data == [{"group": "g1", "records": [rec]}]
