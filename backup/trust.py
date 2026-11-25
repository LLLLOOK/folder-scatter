import os

from PyQt6.QtCore import QSettings
from infra.paths import BACKUP_DIR_NAME, BACKUP_ROOT_NAME
from core.guards import _is_protected_system_dir


def _is_undo_path_allowed(p):
    """撤回记录路径校验：拒绝磁盘根与系统关键目录。
    撤回记录可来自 JSON 文件/本地 IPC，不可全信，防止被伪造后
    借撤回通道搬动系统文件。"""
    if not isinstance(p, str) or not p:
        return False
    drive, rest = os.path.splitdrive(p)
    if not drive or rest in ("", "\\", "/"):
        return False
    return not _is_protected_system_dir(p)


def _is_trusted_backup(bak):
    """备份路径必须属于我们创建的两种布局之一：
    旧式 <父目录>/.scatter_backup/[相对层级/]文件，
    新式 <盘>:/散开备份/<批次>/[相对层级/]文件。
    防伪造记录借「还原备份」搬动任意文件。
    B4 起落点保留源相对层级，标记目录可在任意祖先层（不再贴近文件名），
    严格度不变：仍必须有对应标记目录，新式仍必须有批次层。"""
    if not isinstance(bak, str) or not bak:
        return False
    parts = os.path.normpath(bak).split(os.sep)
    if BACKUP_DIR_NAME in parts[:-1]:
        return True
    if BACKUP_ROOT_NAME in parts:
        idx = parts.index(BACKUP_ROOT_NAME)
        # 需要批次目录层级（散开备份\<批次>\...），与旧判定 parts[-3] 同级严格
        if len(parts) > idx + 2:
            return True
    cr = _custom_backup_root()
    if cr:
        np = os.path.normpath(bak)
        if np == cr or np.startswith(cr + os.sep):
            return True
    return False


def _custom_backup_root():
    """用户自定义备份根目录（设置里勾选启用且目录存在时生效），否则 ""。"""
    try:
        qs = QSettings("FolderPromoter", "FolderPromoter")
        if not bool(qs.value("backup_custom_enabled", False, type=bool)):
            return ""
        p = str(qs.value("backup_custom_path", "") or "").strip()
        if p and os.path.isdir(p):
            return os.path.normpath(p)
    except Exception:
        pass
    return ""
