"""B3 移植测试：走备份的覆盖，落位失败还原旧文件。

缺陷复现：开备份时覆盖流程是「先把旧文件移去备份 → 再放新文件」。源文件
在备份后、落位前被抢删（竞态真实存在）导致落位失败时，旧版无任何还原
逻辑——旧文件留在备份目录成孤儿，原位空了，撤回记录也没生成。
本测试用「src 不存在」真实复现该竞态（不用 mock 落点）：
  · 修复后：备份还原回原位（dst 内容不变）、无撤回记录、日志留痕；
  · 修复前：dst 消失、备份成孤儿 → 断言变红。
"""

import os

import pytest

import backup.vault as vault
import core.fileops as fileops
from core.fileops import move_item


@pytest.fixture
def custom_root(tmp_path, monkeypatch):
    root = tmp_path / "备份根"
    monkeypatch.setattr(vault, "_custom_backup_root", lambda: str(root))
    return root


def _run_overwrite(tmp_path, mode, src_exists):
    """构造覆盖场景并调用 _move_item_impl，返回 (dst, logs, undo_records, root_relative备份列表)。"""
    parent = tmp_path / "p"
    parent.mkdir(exist_ok=True)
    dst = parent / "f.txt"
    dst.write_text("old", encoding="utf-8")
    src = parent / "sub" / "f.txt"
    if src_exists:
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("new", encoding="utf-8")
    logs = []
    undo_records = []
    # 走公开入口 move_item：落位异常按设计上抛给它分类记日志（生产数据流）
    move_item(str(src), str(dst), mode, "overwrite", logs.append,
              undo_records=undo_records, to_trash=True,
              backup_batch="b1", backup_parent=str(parent))
    return dst, logs, undo_records


# ---------------------------------------------------------------- 落位失败还原

def test_move_place_failure_rolls_back_backup(tmp_path, custom_root):
    dst, logs, undo_records = _run_overwrite(tmp_path, "move", src_exists=False)

    assert dst.read_text(encoding="utf-8") == "old", \
        "落位失败必须把备份还原回原位（旧文件不得成孤儿）"
    assert undo_records == [], "落位失败不得生成撤回记录"
    assert any("还原回原位" in m for m in logs)
    # 备份已搬回原位：备份目录不得留下孤儿文件
    leftovers = [p for p in custom_root.rglob("*") if p.is_file()]
    assert leftovers == [], f"备份目录残留孤儿: {leftovers}"


def test_copy_place_failure_rolls_back_backup(tmp_path, custom_root):
    dst, logs, undo_records = _run_overwrite(tmp_path, "copy", src_exists=False)

    assert dst.read_text(encoding="utf-8") == "old"
    assert undo_records == []
    assert any("还原回原位" in m for m in logs)
    leftovers = [p for p in custom_root.rglob("*") if p.is_file()]
    assert leftovers == []


def test_rollback_failure_keeps_backup_and_logs_path(tmp_path, custom_root, monkeypatch):
    """还原也失败：备份保留 + 错误日志含备份完整路径（宁留勿丢）。"""
    monkeypatch.setattr(fileops, "_restore_backup_to", lambda b, d: False)
    dst, logs, undo_records = _run_overwrite(tmp_path, "move", src_exists=False)

    assert not dst.exists(), "还原失败时原位仍为空（本场景语义）"
    leftovers = [p for p in custom_root.rglob("*") if p.is_file()]
    assert len(leftovers) == 1, "备份必须保留"
    assert any("备份还原失败" in m and str(leftovers[0]) in m for m in logs), \
        "错误日志必须含备份完整路径"
    assert undo_records == []


# ---------------------------------------------------------------- 回归：成功路径不受影响

def test_successful_overwrite_unaffected(tmp_path, custom_root):
    dst, logs, undo_records = _run_overwrite(tmp_path, "move", src_exists=True)

    assert dst.read_text(encoding="utf-8") == "new", "新文件正常落位"
    assert len(undo_records) == 1
    bak = undo_records[0].get("backup")
    assert bak and os.path.isfile(bak), "成功覆盖的备份必须保留供撤回还原"
    assert not any("还原回原位" in m for m in logs), "成功路径不得触发还原"
