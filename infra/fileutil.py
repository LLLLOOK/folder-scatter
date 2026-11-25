import os
import re
import shutil
import stat
import time


# ---------- 核心逻辑（复制自 main.py，便于独立运行 / 打包） ----------

def natural_sort_key(s):
    """自然排序：1, 2, 3, 10, 11 而不是 1, 10, 11, 2。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _clear_readonly(path):
    """去掉文件只读属性（Windows 上从压缩包解出的文件常带只读）。"""
    try:
        if os.path.isfile(path):
            os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass


def _safe_op(fn, retries=3, delay=0.5):
    """执行文件操作，遇到占用/临时错误自动重试几次。"""
    last = None
    for _ in range(retries):
        try:
            return fn()
        except (OSError, shutil.Error) as e:
            last = e
            time.sleep(delay)
    raise last


def _lp(p):
    """Windows 长路径：超过 MAX_PATH(260) 时加扩展前缀绕过限制；短路径返回规范绝对路径。
    UNC（\\\\server\\share）须用 \\\\?\\UNC\\ 形式，直接拼 \\\\?\\ 是非法路径。
    只在底层系统调用处使用，日志/撤回仍记录原始路径。"""
    ap = os.path.abspath(os.path.normpath(p))
    if len(ap) >= 260:
        if ap.startswith("\\\\"):
            return "\\\\?\\UNC\\" + ap.lstrip("\\")
        return "\\\\?\\" + ap
    return ap


def _unique_dst(dst):
    """目标已存在时，返回 原名-1.后缀 / 原名-2.后缀 ... 形式的空闲路径。

    若 原名-1 已存在，会自动检索到最大序号后生成 -2、-3，绝不产生 -1-1。
    """
    if not os.path.exists(dst):
        return dst
    base, ext = os.path.splitext(dst)
    i = 1
    while os.path.exists(f"{base}-{i}{ext}"):
        i += 1
    return f"{base}-{i}{ext}"


def _is_hidden(path):
    """判断文件/文件夹是否为系统隐藏。

    Windows：通过 stat 的 st_file_attributes 检测 FILE_ATTRIBUTE_HIDDEN(=2)。
    其他平台（或取不到属性）：以 '.' 开头的视为隐藏。"""
    try:
        st = os.stat(path)
        if hasattr(st, "st_file_attributes"):
            return bool(st.st_file_attributes & 2)
    except OSError:
        pass
    return os.path.basename(path).startswith(".")


def _classify_oserror(e):
    """把 OSError/shutil.Error 归类，用于总结统计：
    'occupied'   文件被占用（另一个进程正在使用，WinError 32）
    'permission' 权限不足（拒绝访问，WinError 5 / PermissionError）
    'other'      其他
    """
    winerror = getattr(e, "winerror", None)
    if winerror == 32:
        return "occupied"
    if winerror == 5 or isinstance(e, PermissionError):
        return "permission"
    s = str(e).lower()
    if "being used" in s or "in use" in s or "另一个程序" in s or "被占用" in s:
        return "occupied"
    if "denied" in s or "permission" in s:
        return "permission"
    return "other"


def _oserr_text(e):
    """OSError 日志文案简化：只取系统描述（如"系统找不到指定的路径。"），
    不带 [WinError N] 前缀与重复的路径尾巴；目标不存在时补一句刷新提示。"""
    msg = e.strerror or str(e)
    if isinstance(e, FileNotFoundError):
        msg += "（可能已被删除，请刷新目录树）"
    return msg
