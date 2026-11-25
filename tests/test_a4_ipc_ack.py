"""A4 移植测试：IPC 应用层 ack。

缺陷复现：旧版静默进程发完 done 就认为成功；GUI「已收字节、未落盘」之间
崩溃，记录丢失且双方都以为没事；重复投递也无幂等去重。
修复后期望：
  · GUI 落盘成功才回 ack（忙碌延后/保存暂缓不回）；
  · 同一 batch_id 重复投递只回 ack 不再压栈；
  · 静默端收不到 ack 一律降级写 pending；
  · 启动合并按内容去重，重复落盘的同批只留一份。
"""

import json
import threading
import types

import pytest
from PyQt6.QtNetwork import QLocalSocket
from PyQt6.QtTest import QTest

import undo.history as uh
import undo.store as undo_store
from undo.history import UndoHistoryStore
import ui.main_window as mw
from infra.paths import IPC_NAME


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def app(tmp_path, monkeypatch, qapp):
    """真实 App（offscreen），撤回历史/pending 重定向到 tmp，防污染工作区。"""
    monkeypatch.setattr(uh, "UNDO_HISTORY_FILE", str(tmp_path / "undo_history.json"))
    monkeypatch.setattr(uh, "UNDO_PENDING_FILE", str(tmp_path / "pending.json"))
    w = mw.App()
    yield w
    # 注意：不能调 w.close()——closeEvent 会走 sys.exit(0) 直接退掉 pytest 进程
    w.deleteLater()
    QTest.qWait(100)


def _send(sock, obj):
    sock.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    sock.flush()


def _done_msg(bid, recs, group="g1", parent=None):
    return {"type": "done", "parent_dir": parent, "undo_records": recs,
            "group": group, "batch_id": bid}


def _rec(name="a.txt"):
    return {"src": rf"D:\p\{name}", "dst": rf"D:\{name}", "mode": "move"}


def _connect():
    s = QLocalSocket()
    s.connectToServer(IPC_NAME)
    assert s.waitForConnected(2000), "连不上 GUI 的 IPC 服务"
    return s


def _flush_now(app):
    """确定性触发防抖落盘（不等 300ms 定时器）。"""
    app._ipc_done_timer.stop()
    app._ipc_flush_done()


def _read_reply(sock, timeout=3000):
    """事件循环内等 ack（直接 waitForReadyRead 会卡住同线程的服务端）。"""
    waited = 0
    while waited < timeout:
        QTest.qWait(50)
        waited += 50
        if sock.bytesAvailable():
            return bytes(sock.readAll())
    return b""


# ---------------------------------------------------------------- ack 与幂等

def test_ack_after_persist_and_dedup(app):
    recs = [_rec()]
    s1 = _connect()
    _send(s1, _done_msg("bid-1", recs))
    QTest.qWait(100)          # 让服务端 readyRead → 进防抖缓冲
    _flush_now(app)
    assert b"ack" in _read_reply(s1), "落盘成功必须回 ack"
    assert len(app.undo_stack) == 1
    hist = json.loads(open(uh.UNDO_HISTORY_FILE, encoding="utf-8").read())
    assert hist == [recs], "批次必须已持久化"

    # 同一 batch_id 重复投递（静默端没等到 ack 重发）：只回 ack 不再压栈
    s2 = _connect()
    _send(s2, _done_msg("bid-1", recs))
    assert b"ack" in _read_reply(s2), "重复投递也要回 ack（幂等）"
    assert len(app.undo_stack) == 1, "同一 batch_id 不得压栈两次"
    s1.abort()
    s2.abort()


def test_no_ack_when_busy_deferred(app):
    """本地散开进行中：批次延后压栈（未落盘）→ 不得回 ack。"""
    app.worker = types.SimpleNamespace(isRunning=lambda: True)  # 假装忙碌
    try:
        s = _connect()
        _send(s, _done_msg("bid-busy", [_rec("b.txt")]))
        QTest.qWait(100)
        _flush_now(app)
        assert _read_reply(s, 800) == b"", "未落盘不得回 ack"
        assert app.undo_stack == []
        assert len(app._deferred_undo) == 1, "批次应挂起延后"
        s.abort()
    finally:
        app.worker = None


# ---------------------------------------------------------------- 静默端：无 ack 降级 pending / 收到 ack 不写 pending

def _run_silent_in_thread(tmp_path, monkeypatch):
    """受控替身跑 run_silent_folder（真实 IPC 连接本进程 GUI），
    返回 (线程, pending文件路径, append调用记录)。"""
    import main

    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
    monkeypatch.setattr(main, "_scatter_group_id", lambda parent: ("g1", False))
    monkeypatch.setattr(main, "_new_backup_batch", lambda: "batch-test")
    monkeypatch.setattr(main, "_is_protected_system_dir", lambda p: False)
    pend = tmp_path / "pending.json"
    monkeypatch.setattr(undo_store, "UNDO_PENDING_FILE", str(pend))
    (tmp_path / "error.log").write_text("", encoding="utf-8")

    rec = {"src": str(tmp_path / "p" / "f.txt"),
           "dst": str(tmp_path / "f.txt"), "mode": "move"}

    def fake_scatter(selected, parent_dir, mode, conflict, suffixes,
                     max_size_mb, simulate, emit_log, countfn, skipfn,
                     stop_check, undo_records, **kw):
        undo_records.append(rec)
        return 1, len(selected)

    monkeypatch.setattr(main, "execute_scatter", fake_scatter)

    sub = tmp_path / "p" / "sub"
    sub.mkdir(parents=True)
    (sub / "f.txt").write_text("x", encoding="utf-8")

    t = threading.Thread(
        target=lambda: main.run_silent_folder(str(sub), "one"), daemon=True)
    return t, pend, rec


def test_silent_writes_pending_when_no_gui(tmp_path, monkeypatch, qapp):
    """GUI 不在线（无 ack）：静默端必须降级写 pending（真实复现缺陷场景）。"""
    t, pend, rec = _run_silent_in_thread(tmp_path, monkeypatch)
    t.start()
    t.join(30)
    assert not t.is_alive()
    data = json.loads(pend.read_text(encoding="utf-8"))
    assert data == [{"group": "g1", "records": [rec]}]


def test_silent_no_pending_when_ack_received(tmp_path, monkeypatch, app):
    """GUI 在线并落盘回 ack：静默端不写 pending；GUI 栈内有且只有一批。"""
    t, pend, rec = _run_silent_in_thread(tmp_path, monkeypatch)
    t.start()
    # 主线程泵事件：服务端接收 done → 300ms 防抖 → 落盘 → 回 ack
    waited = 0
    while t.is_alive() and waited < 15000:
        QTest.qWait(100)
        waited += 100
    t.join(5)
    assert not t.is_alive(), "静默端未在限定时间结束"
    assert not pend.exists(), "收到 ack 不得再写 pending"
    assert any(rec in batch for batch in app.undo_stack), \
        "批次必须经 IPC 在 GUI 落盘"


# ---------------------------------------------------------------- 启动合并内容去重

def test_startup_merge_dedups_repeated_batch(tmp_path, monkeypatch):
    """同批两处落盘（GUI 已保存 + 静默端无 ack 又写 pending）：
    启动合并后只保留一份。"""
    hist = tmp_path / "undo_history.json"
    pend = tmp_path / "pending.json"
    rec = _rec("c.txt")
    hist.write_text(json.dumps([[rec]], ensure_ascii=False), encoding="utf-8")
    pend.write_text(json.dumps([{"group": "g1", "records": [rec]}],
                               ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(uh, "UNDO_HISTORY_FILE", str(hist))
    monkeypatch.setattr(uh, "UNDO_PENDING_FILE", str(pend))
    msgs = []
    fake = types.SimpleNamespace(undo_store=UndoHistoryStore(msgs.append))

    mw.App._load_undo_history(fake)

    assert fake.undo_store.stack == [[rec]], "重复批次必须被去重，只留一份"
    assert any("重复" in m for m in msgs)
    assert not pend.exists()
