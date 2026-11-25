"""A3 移植测试：撤回幂等重入。

缺陷复现：同一批次被重复撤回（或崩溃后重试在途批次）时，旧版无
「dst 已不在 + src 已在 = 上次已还原」的幂等判定——
  · move 模式误报「无法自动还原」刷警告；
  · copy 模式误报「副本已不存在」。
修复后期望：已还原项静默跳过、记录正常出列；真正丢失（src 也不在）仍告警。
把修复回退（删掉幂等分支）时 test_*_silent 用例应变红。
"""

import os

import pytest
from PyQt6.QtCore import Qt

import undo.restore as restore_mod
from undo.restore import UndoWorker


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def allow_tmp_paths(monkeypatch):
    # pytest tmp 在 LOCALAPPDATA 下，命中系统保护目录防线；
    # 本用例不验证路径防线，放行（防线本身由既有逻辑保障）
    monkeypatch.setattr(restore_mod, "_is_undo_path_allowed", lambda p: True)


def _run_batch(batch):
    """同步执行撤回 worker（直接调 run() 在本线程跑完），返回 (worker, 日志列表)。"""
    w = UndoWorker(batch)
    logs = []
    w.logmsg.connect(logs.append, Qt.ConnectionType.DirectConnection)
    w.run()
    return w, logs


def _warns(logs):
    return [m for m in logs if m.startswith("[警告]")]


# ---------------------------------------------------------------- move 分支

def test_move_already_restored_is_silent(tmp_path, qapp):
    """幂等主场景：dst 已不在散开位置、src 已在原位 = 上次已还原 → 静默跳过。"""
    src = tmp_path / "orig" / "f.txt"
    dst = tmp_path / "f.txt"
    src.parent.mkdir(parents=True)
    src.write_text("data", encoding="utf-8")
    assert not dst.exists()
    rec = {"src": str(src), "dst": str(dst), "mode": "move"}

    w, logs = _run_batch([rec])

    assert not any("无法自动还原" in m for m in _warns(logs)), \
        f"已还原项不得误报无法自动还原: {_warns(logs)}"
    assert src.read_text(encoding="utf-8") == "data", "原文件不得被移动"
    assert w.failed == [] and w.remaining == [], "幂等项应正常出列、不留栈"


def test_move_already_restored_with_backup_still_restores_old_file(tmp_path, qapp):
    """幂等 + 备份：新文件已还原（静默），被覆盖的旧文件备份仍照常还原。"""
    src = tmp_path / "orig" / "f.txt"
    dst = tmp_path / "f.txt"
    bak = tmp_path / "散开备份" / "b1" / "f.txt"
    src.parent.mkdir(parents=True)
    bak.parent.mkdir(parents=True)
    src.write_text("new", encoding="utf-8")
    bak.write_text("old", encoding="utf-8")
    rec = {"src": str(src), "dst": str(dst), "mode": "move", "backup": str(bak)}

    w, logs = _run_batch([rec])

    assert not any("无法自动还原" in m or "无法还原新文件" in m for m in _warns(logs))
    assert dst.read_text(encoding="utf-8") == "old", "旧文件备份应还原到 dst"
    assert not bak.exists()
    assert src.read_text(encoding="utf-8") == "new"
    assert w.failed == [] and w.remaining == []


def test_move_truly_missing_still_warns(tmp_path, qapp):
    """真丢失（dst 与 src 都不在）不属幂等：仍须告警，不得被静默吞掉。"""
    src = tmp_path / "orig" / "gone.txt"
    dst = tmp_path / "gone.txt"
    rec = {"src": str(src), "dst": str(dst), "mode": "move"}

    w, logs = _run_batch([rec])

    assert any("无法自动还原" in m for m in _warns(logs))
    assert w.failed == [] and w.remaining == []  # 不可重试：告警后出列（既有语义）


def test_move_normal_restore_still_works(tmp_path, qapp):
    """常规撤回回归：dst 在、src 不在 → 搬回原位。"""
    src = tmp_path / "orig" / "f.txt"
    dst = tmp_path / "f.txt"
    dst.write_text("data", encoding="utf-8")
    rec = {"src": str(src), "dst": str(dst), "mode": "move"}

    w, logs = _run_batch([rec])

    assert src.read_text(encoding="utf-8") == "data"
    assert not dst.exists()
    assert any("已还原" in m for m in logs)
    assert w.failed == [] and w.remaining == []


# ---------------------------------------------------------------- copy 分支

def test_copy_copygone_is_silent(tmp_path, qapp):
    """copy 幂等：副本已不在（上次撤回已送回收站）→ 视为已处理，不告警。"""
    src = tmp_path / "orig" / "f.txt"
    dst = tmp_path / "f.txt"
    rec = {"src": str(src), "dst": str(dst), "mode": "copy"}

    w, logs = _run_batch([rec])

    assert not any("副本已不存在" in m for m in _warns(logs)), \
        f"副本已不在不得告警: {_warns(logs)}"
    assert w.failed == [] and w.remaining == []


def test_copy_copygone_with_backup_restores_old_file_silently(tmp_path, qapp):
    """copy 幂等 + 备份：副本已不在不告警，旧文件备份照常还原。"""
    src = tmp_path / "orig" / "f.txt"
    dst = tmp_path / "f.txt"
    bak = tmp_path / "散开备份" / "b1" / "f.txt"
    bak.parent.mkdir(parents=True)
    bak.write_text("old", encoding="utf-8")
    rec = {"src": str(src), "dst": str(dst), "mode": "copy", "backup": str(bak)}

    w, logs = _run_batch([rec])

    assert not any("副本已不存在" in m for m in _warns(logs))
    assert dst.read_text(encoding="utf-8") == "old"
    assert any("已还原被覆盖的旧文件" in m for m in logs)
    assert w.failed == [] and w.remaining == []
