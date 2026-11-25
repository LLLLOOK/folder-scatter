"""备份维护（纯函数组）：按天保留清理 / 孤儿备份清理 / 清空备份根 / 备份目录收集。

从 ui/main_window.py 的 App 类抽出的备份维护逻辑（第二轮拆分第 2 步），
函数体与原 App 实现逐字一致（只搬不改）：`self.undo_stack` → 参数 `stack`，
`self.log_msg` → 参数 `log`。纯 Python、不 import Qt，可被 tests 直接单测。

签名统一 `(stack, log)`：stack 由 App 传 `self.undo_store.stack`，log 传
`self.log_msg`；不用 log/stack 的函数也保持同序参数，维持接口一致。
复用 vault/trust 现有原语，备份布局与信任校验语义不变。
"""

import os
import time

import send2trash

from infra.paths import BACKUP_ROOT_NAME
from backup.trust import _is_trusted_backup
from backup.vault import _iter_backup_roots, _rmdir_if_empty


def apply_backup_retention(stack, log, days):
    """按天保留（设置 backup_max_days，0=关闭）：删除超期的记录引用备份。
    被删的覆盖将不可自动还原——日志明确告知。
    days 由调用方从设置读出传入（QSettings 读取留在 App 薄壳）。"""
    if days <= 0:
        return
    cutoff = time.time() - days * 86400
    purged = 0
    for batch in stack:
        if not isinstance(batch, list):
            continue
        for rec in batch:
            bak = rec.get("backup") if isinstance(rec, dict) else None
            if isinstance(bak, str) and _is_trusted_backup(bak):
                try:
                    if os.path.isfile(bak) and os.path.getmtime(bak) < cutoff:
                        send2trash.send2trash(bak)
                        purged += 1
                except OSError:
                    pass
    if purged:
        log(f"[备份] 按天保留已开启：{purged} 个超期备份"
            f"（超过 {days} 天）已移入回收站，相应覆盖不再自动还原。")


def _rmdir_empty_tree(root):
    """自底向上清掉 root 下所有空目录（ deepest-first 天然级联）；
    root 本身是「散开备份」标记根且空了也一并删除。非空目录自然保留。"""
    nr = os.path.normcase(os.path.normpath(root))
    for dp, _ds, _fs in os.walk(root, topdown=False):
        if os.path.normcase(os.path.normpath(dp)) == nr:
            continue  # root 本身按标记名规则处理
        _rmdir_if_empty(dp)
    if os.path.basename(os.path.normpath(root)) == BACKUP_ROOT_NAME:
        _rmdir_if_empty(root)


def clean_orphan_backups(stack, log, roots=None):
    """手动清理失效备份：删除未被当前撤回栈引用的备份文件（淘汰批次残留等），
    再逐级清理空批次目录/根目录。只删备份布局内的文件，返回删除数。
    roots 为 None 时扫描全部备份根目录。
    B4：备份落点带源相对层级（批次目录\\<相对路径>），必须 os.walk 递归
    遍历——只扫「根\\批次\\」一层会把嵌套孤儿备份永远漏掉。"""
    referenced = set()
    for batch in stack:
        if not isinstance(batch, list):
            continue
        for rec in batch:
            if isinstance(rec, dict) and isinstance(rec.get("backup"), str):
                # normcase+normpath：Windows 路径大小写/斜杠不敏感，
                # 精确字符串比较会把仍被引用的备份误判成孤儿物理删除
                referenced.add(os.path.normcase(os.path.normpath(rec["backup"])))
    removed = 0
    for root in (roots if roots is not None else _iter_backup_roots()):
        for dp, _ds, fs in os.walk(root):
            for name in fs:
                fp = os.path.join(dp, name)
                if (os.path.normcase(os.path.normpath(fp)) in referenced
                        or not os.path.isfile(fp)):
                    continue
                try:
                    os.remove(fp)
                    removed += 1
                except OSError:
                    pass
        _rmdir_empty_tree(root)
    return removed


def empty_backup_root(stack, log, root):
    """显式清空某备份根目录：全部备份文件移入回收站（可找回），
    再自底向上清理空目录；「散开备份」盘根空了也删。返回移入数。
    B4：落点带源相对层级（批次目录\\<相对路径>），必须 os.walk 递归——
    只扫一层会漏删嵌套备份，还对外声称「已清空」。"""
    moved = 0
    for dp, _ds, fs in os.walk(root):
        for name in fs:
            fp = os.path.join(dp, name)
            if not os.path.isfile(fp):
                continue
            try:
                send2trash.send2trash(fp)
                moved += 1
            except OSError:
                pass
    _rmdir_empty_tree(root)
    return moved


def collect_backup_dirs(stack, log):
    """汇总撤回栈仍引用、且文件确实存在的备份目录 -> {目录: 文件数}。"""
    dirs = {}
    for batch in stack:
        if not isinstance(batch, list):
            continue
        for rec in batch:
            bak = rec.get("backup") if isinstance(rec, dict) else None
            if isinstance(bak, str) and os.path.isfile(bak):
                d = os.path.dirname(bak)
                dirs[d] = dirs.get(d, 0) + 1
    return dirs
