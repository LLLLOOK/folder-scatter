"""B4 移植测试：备份保留源相对路径。

缺陷复现：旧版备份落点只用纯文件名（批次目录\\basename），同批 a\\1.tif 与
b\\1.tif 备份后变成 1.tif、1-1.tif，目录归属不可辨。
修复后期望：落点 = 批次目录\\<dst 相对散开父目录的路径>；越界退回纯文件名；
撞名仍 _unique_dst 兜底；信任校验 / 撤回还原 / 空目录清理全部兼容相对层级。
"""

import os

import pytest
from PyQt6.QtCore import Qt

import backup.vault as vault
import backup.trust as trust
import undo.restore as restore_mod
from backup.vault import _backup_overwritten, _purge_batch_backups, _safe_rel
from undo.restore import UndoWorker


@pytest.fixture
def custom_root(tmp_path, monkeypatch):
    """自定义备份根落在 tmp（不打注册表，直接替身 _custom_backup_root）。"""
    root = tmp_path / "备份根"
    monkeypatch.setattr(vault, "_custom_backup_root", lambda: str(root))
    return root


# ---------------------------------------------------------------- 落点构造

def test_safe_rel_basic(tmp_path):
    parent = str(tmp_path)
    assert _safe_rel(os.path.join(parent, "sub", "f.txt"), parent) == \
        os.path.join("sub", "f.txt")
    assert _safe_rel(os.path.join(parent, "f.txt"), parent) == "f.txt"


def test_safe_rel_fallbacks(tmp_path):
    parent = str(tmp_path)
    # 不在父目录下 / 父目录为空 → 纯文件名保底
    assert _safe_rel(r"C:\elsewhere\f.txt", parent) == "f.txt"
    assert _safe_rel(r"C:\any\f.txt", "") == "f.txt"


def test_backup_keeps_relative_path(tmp_path, custom_root):
    parent = tmp_path / "p"
    dst = parent / "sub" / "f.txt"
    dst.parent.mkdir(parents=True)
    dst.write_text("old", encoding="utf-8")

    bp = _backup_overwritten(str(dst), batch="b1", scatter_parent=str(parent))

    assert bp == str(custom_root / "b1" / "sub" / "f.txt")
    with open(bp, "r", encoding="utf-8") as f:
        assert f.read() == "old"
    assert not dst.exists()


def test_backup_same_name_two_dirs_no_confusion(tmp_path, custom_root):
    """缺陷主场景：同批 a\\1.tif 与 b\\1.tif 归属必须可辨、互不吞。"""
    parent = tmp_path / "p"
    da = parent / "a" / "1.tif"
    db = parent / "b" / "1.tif"
    da.parent.mkdir(parents=True)
    db.parent.mkdir(parents=True)
    da.write_text("A", encoding="utf-8")
    db.write_text("B", encoding="utf-8")

    ba = _backup_overwritten(str(da), batch="b1", scatter_parent=str(parent))
    bb = _backup_overwritten(str(db), batch="b1", scatter_parent=str(parent))

    assert ba == str(custom_root / "b1" / "a" / "1.tif")
    assert bb == str(custom_root / "b1" / "b" / "1.tif")
    with open(ba, encoding="utf-8") as f:
        assert f.read() == "A"
    with open(bb, encoding="utf-8") as f:
        assert f.read() == "B"


def test_backup_same_rel_collision_unique(tmp_path, custom_root):
    """同一相对路径撞名仍 _unique_dst 追加，不互吞。"""
    parent = tmp_path / "p"
    dst = parent / "sub" / "f.txt"
    dst.parent.mkdir(parents=True)
    dst.write_text("one", encoding="utf-8")
    b1 = _backup_overwritten(str(dst), batch="b1", scatter_parent=str(parent))
    dst.write_text("two", encoding="utf-8")
    b2 = _backup_overwritten(str(dst), batch="b1", scatter_parent=str(parent))
    assert b1 != b2 and os.path.isfile(b1) and os.path.isfile(b2)


# ---------------------------------------------------------------- 信任校验兼容

@pytest.mark.parametrize("path,ok", [
    (r"C:\散开备份\b1\f.txt", True),            # 旧新式布局
    (r"C:\散开备份\b1\sub\f.txt", True),        # B4 相对层级
    (r"C:\散开备份\f.txt", False),              # 缺批次层（与旧判定同级严格）
    (r"D:\data\.scatter_backup\f.txt", True),   # 旧式布局
    (r"D:\data\.scatter_backup\sub\f.txt", True),  # B4 相对层级
    (r"C:\other\b1\f.txt", False),              # 无标记目录
])
def test_trust_nested_layouts(path, ok, monkeypatch):
    monkeypatch.setattr(trust, "_custom_backup_root", lambda: "")
    assert trust._is_trusted_backup(path) is ok


# ---------------------------------------------------------------- 撤回还原 + 空目录清理

@pytest.fixture
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def allow_tmp_paths(monkeypatch):
    monkeypatch.setattr(restore_mod, "_is_undo_path_allowed", lambda p: True)


def test_undo_restores_nested_backup_and_cleans_empty_chain(tmp_path, qapp, allow_tmp_paths):
    """相对层级备份：撤回能还原（信任校验通过），且空子目录壳链逐级清掉。"""
    bak_root = tmp_path / "散开备份"
    bak = bak_root / "b1" / "sub" / "f.txt"
    bak.parent.mkdir(parents=True)
    bak.write_text("old", encoding="utf-8")
    src = tmp_path / "orig" / "f.txt"
    dst = tmp_path / "sub" / "f.txt"
    dst.parent.mkdir(parents=True)
    dst.write_text("new", encoding="utf-8")
    rec = {"src": str(src), "dst": str(dst), "mode": "move", "backup": str(bak)}

    w = UndoWorker([rec])
    logs = []
    w.logmsg.connect(logs.append, Qt.ConnectionType.DirectConnection)
    w.run()

    assert src.read_text(encoding="utf-8") == "new", "新文件应还原回 src"
    assert dst.read_text(encoding="utf-8") == "old", "相对层级备份应还原回 dst"
    assert not bak.exists()
    assert not bak_root.exists(), "空子目录/批次目录/空根必须逐级清掉"
    assert w.failed == [] and w.remaining == []


def test_purge_batch_backups_cleans_nested(tmp_path):
    bak_root = tmp_path / "散开备份"
    bak = bak_root / "b1" / "sub" / "f.txt"
    bak.parent.mkdir(parents=True)
    bak.write_text("old", encoding="utf-8")

    _purge_batch_backups([[{"backup": str(bak)}]])

    assert not bak.exists()
    assert not bak_root.exists(), "空壳链必须逐级清掉"


def test_purge_keeps_nonempty_backup_root(tmp_path):
    bak_root = tmp_path / "散开备份"
    bak = bak_root / "b1" / "sub" / "f.txt"
    other = bak_root / "b2" / "g.txt"
    bak.parent.mkdir(parents=True)
    other.parent.mkdir(parents=True)
    bak.write_text("old", encoding="utf-8")
    other.write_text("keep", encoding="utf-8")

    _purge_batch_backups([[{"backup": str(bak)}]])

    assert not bak.exists()
    assert other.exists(), "其他批次的备份不得被动"
    assert (bak_root / "b2").exists()
    assert not (bak_root / "b1").exists(), "已空批次目录应清掉"
