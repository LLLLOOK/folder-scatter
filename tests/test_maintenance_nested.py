"""F3 回归：清空/清理备份必须识别 B4 嵌套相对层级布局（批次\\子目录\\文件）。

B4 起 _backup_overwritten 落点为 批次目录\\<dst 相对散开父目录的路径>，
合并文件夹发生覆盖时会产生嵌套层级；maintenance 两函数若只扫「根\\批次\\」
一层，会漏删嵌套备份却声称「已清空」，并致撤回批次被过度出栈。
send2trash 一律打桩为「记录+删除」，不碰真实回收站。
"""

import os

import backup.maintenance as bm
from backup.maintenance import clean_orphan_backups, empty_backup_root


def _mk(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")


def _fake_trash(monkeypatch):
    trashed = []

    def _t(p):
        trashed.append(p)
        os.remove(p)

    monkeypatch.setattr(bm.send2trash, "send2trash", _t)
    return trashed


def test_empty_backup_root_nested(tmp_path, monkeypatch):
    trashed = _fake_trash(monkeypatch)
    root = tmp_path / "散开备份"
    _mk(root / "b1" / "sub" / "deep" / "a.txt")  # B4 嵌套落点
    _mk(root / "b1" / "top.txt")                 # 批次目录直接子层
    _mk(root / "old.txt")                        # 旧布局：直接在根下

    n = empty_backup_root([], lambda m: None, str(root))

    assert n == 3
    assert len(trashed) == 3
    assert not root.exists(), "清空后空目录壳（含嵌套）与标记根必须一并删除"


def test_empty_backup_root_custom_root_kept(tmp_path, monkeypatch):
    _fake_trash(monkeypatch)
    root = tmp_path / "mybackups"  # 非「散开备份」标记名的自定义根
    _mk(root / "b1" / "sub" / "a.txt")

    n = empty_backup_root([], lambda m: None, str(root))

    assert n == 1
    assert root.exists(), "自定义根只清空内容，根目录本身保留"
    assert not any(root.iterdir()), "根下空目录壳必须清干净"


def test_clean_orphan_backups_nested(tmp_path):
    root = tmp_path / "散开备份"
    keep = root / "b1" / "sub" / "keep.txt"              # 嵌套且仍被引用
    orphan_deep = root / "b1" / "sub" / "deep" / "o.txt"  # 嵌套孤儿
    orphan_top = root / "b2" / "orphan.txt"
    for p in (keep, orphan_deep, orphan_top):
        _mk(p)
    stack = [[{"backup": str(keep), "src": "x", "dst": "y"}]]

    n = clean_orphan_backups(stack, lambda m: None, roots=[str(root)])

    assert n == 2, "嵌套孤儿也必须清到"
    assert keep.exists(), "仍被撤回栈引用的备份不得误删"
    assert not (root / "b2").exists(), "清空的批次目录必须移除"
    assert not (root / "b1" / "sub" / "deep").exists(), "嵌套空壳链必须级联清理"
    assert (root / "b1" / "sub").exists(), "非空目录链保留"


def test_clean_orphan_backups_case_insensitive_reference(tmp_path):
    """Windows 路径大小写不敏感：引用记录与磁盘实际大小写不同也不得误删。"""
    root = tmp_path / "散开备份"
    keep = root / "b1" / "Sub" / "Keep.TXT"
    _mk(keep)
    stack = [[{"backup": str(keep).lower(), "src": "x", "dst": "y"}]]

    n = clean_orphan_backups(stack, lambda m: None, roots=[str(root)])

    assert n == 0
    assert keep.exists()
