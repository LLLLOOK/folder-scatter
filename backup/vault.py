import os
import shutil
import time
import ctypes
from infra.fileutil import _lp, _safe_op, _unique_dst
from infra.paths import BACKUP_DIR_NAME, BACKUP_ROOT_NAME
from backup.trust import _custom_backup_root, _is_trusted_backup


def _backup_root_of(dst):
    """备份根目录：启用自定义时为自定义目录（跨盘则备份走复制，设置里有提示）；
    否则为 dst 所在盘的 <drive>:\\散开备份。无盘符路径（UNC 等）返回 ""。"""
    cr = _custom_backup_root()
    if cr:
        return cr
    drive, _ = os.path.splitdrive(dst)
    if not drive or drive.startswith("\\\\"):
        return ""
    return os.path.join(drive + os.sep, BACKUP_ROOT_NAME)


def _safe_rel(dst, parent):
    """dst 相对散开父目录的路径（B4）；parent 为空或 dst 不在其下时
    退回纯文件名保底（不越界、不产生 .. 层级）。"""
    if not parent:
        return os.path.basename(dst)
    try:
        rel = os.path.relpath(dst, parent)
    except ValueError:
        return os.path.basename(dst)
    if rel.startswith(".."):
        return os.path.basename(dst)
    return rel


def _backup_overwritten(dst, batch="", scatter_parent=""):
    """覆盖前把旧文件 dst 移入同盘的 <drive>:\\散开备份\\<批次>\\（同盘 rename 瞬时，
    不复制大文件），返回备份路径；任何失败返回 ""（调用方回退送回收站）。
    B4：落点保留 dst 相对散开父目录的层级（批次目录\\<相对路径>），同批
    a\\1.tif 与 b\\1.tif 备份后归属可辨；撞名仍由 _unique_dst 兜底不互吞。"""
    root = _backup_root_of(dst)
    if not root:
        return ""
    bdir = os.path.join(root, batch) if batch else root
    try:
        os.makedirs(bdir, exist_ok=True)
    except OSError:
        # 盘根无写权限（如标准用户在 C 盘）：回退旧式父目录隐藏备份，保护不丢
        bdir = os.path.join(os.path.dirname(dst), BACKUP_DIR_NAME)
        try:
            os.makedirs(bdir, exist_ok=True)
        except OSError:
            return ""  # 两级都失败：调用方回退送回收站
    bp = os.path.join(bdir, _safe_rel(dst, scatter_parent))
    try:
        os.makedirs(os.path.dirname(bp), exist_ok=True)
    except OSError:
        # 相对层级建不出来（异常字符/权限）：退回批次目录下纯文件名，保护不丢
        bp = os.path.join(bdir, os.path.basename(dst))
    bp = _unique_dst(bp)
    try:
        os.rename(_lp(dst), _lp(bp))
    except OSError:
        try:
            _safe_op(lambda: shutil.move(_lp(dst), _lp(bp)))
        except Exception:
            return ""
    return bp


def _new_backup_batch():
    """本次操作的备份批次子目录名：时间戳+进程号，便于按批汇总与整批清理。"""
    return time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid():x}"


def _restore_backup_to(backup, dst):
    """B3：落位失败时把备份搬回 dst 原位（目录骨架重建），返回是否成功。
    先同盘 rename（瞬时），失败退化 shutil.move；两级都失败返回 False，
    调用方保留备份并报完整路径（任何失败残留「宁留勿丢」）。"""
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
    except OSError:
        pass
    try:
        os.rename(_lp(backup), _lp(dst))
        return True
    except OSError:
        try:
            _safe_op(lambda: shutil.move(_lp(backup), _lp(dst)))
            return True
        except Exception:
            return False


def _count_backup_root(root):
    """统计备份根目录下文件数与总字节数（供设置界面展示）。"""
    n = 0
    size = 0
    for _dp, _ds, fs in os.walk(root):
        for f in fs:
            try:
                size += os.path.getsize(os.path.join(_dp, f))
                n += 1
            except OSError:
                pass
    return n, size


def _iter_backup_roots():
    """枚举备份根目录：自定义根（若启用）+ 各盘符下已创建的「散开备份」。"""
    roots = []
    cr = _custom_backup_root()
    if cr:
        roots.append(cr)
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return roots
    for i in range(26):
        if mask & (1 << i):
            p = os.path.join(chr(ord("A") + i) + ":\\", BACKUP_ROOT_NAME)
            if os.path.isdir(p) and os.path.normcase(p) not in \
                    {os.path.normcase(r) for r in roots}:
                roots.append(p)
    return roots


def _rmdir_if_empty(path):
    """目录存在且为空才删，返回是否删了。"""
    try:
        if os.path.isdir(path) and not os.listdir(path):
            os.rmdir(path)
            return True
    except OSError:
        pass
    return False


def _rmdir_backup_ancestors(d):
    """从备份文件所在目录逐级向上清理空目录，直到遇到非空目录或
    越过备份根（「散开备份」根 / .scatter_backup 本身）为止。
    B4 起备份落点带源相对层级（批次目录\\<相对路径>），只清一层会留下
    空子目录壳链；无相对层级的旧布局行为不变。"""
    while _rmdir_if_empty(d):
        if os.path.basename(d) in (BACKUP_ROOT_NAME, BACKUP_DIR_NAME):
            break
        parent = os.path.dirname(d)
        if not parent or parent == d:
            break
        d = parent


def _purge_batch_backups(batches):
    """删除这些批次记录引用的备份文件（批次离开撤回栈 = 不可再撤回，
    备份失去意义），再逐级清理空批次目录/根目录。
    只删记录引用过的文件，避免误删用户自行放入目录的东西。"""
    touched = set()
    for batch in batches:
        if not isinstance(batch, list):
            continue
        for rec in batch:
            bak = rec.get("backup") if isinstance(rec, dict) else None
            if isinstance(bak, str) and _is_trusted_backup(bak):
                touched.add(os.path.dirname(bak))
                try:
                    if os.path.isfile(bak):
                        os.remove(bak)
                except OSError:
                    pass
    for d in touched:
        _rmdir_backup_ancestors(d)  # B4：逐级清掉空子目录/批次目录/空根
