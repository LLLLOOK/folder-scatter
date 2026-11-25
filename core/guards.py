import os
import stat
import ctypes


def _is_link_or_junction(path):
    """判断路径是否为符号链接 / junction / 挂载点（reparse point）。
    递归类操作（合并、copytree、count_files、目录树扫描、os.walk）必须对其止步，
    否则会穿透到链接目标位置搬动/复制别的目录的内容，或被循环链接绕死递归。"""
    try:
        if os.path.islink(path):
            return True
        isj = getattr(os.path, "isjunction", None)  # Python 3.12+
        if isj is not None and isj(path):
            return True
        st = os.lstat(path)
        # FILE_ATTRIBUTE_REPARSE_POINT = 0x400（覆盖挂载点等 islink/isjunction 未识别的类型）
        return bool(getattr(st, "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


# ---------- 系统关键目录保护（纵深防御：防误把 Windows/Program Files 等散开导致系统崩溃） ----------
def _build_protected_system_dirs():
    """构建受保护系统目录集合：优先用环境变量（兼容系统盘非 C:），并硬编码兜底。
    命中规则：路径 == 某受保护目录，或位于其下任意层级。"""
    dirs = set()
    for ev in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)",
              "ProgramData", "APPDATA", "LOCALAPPDATA"):
        v = os.environ.get(ev)
        if v:
            dirs.add(os.path.normcase(os.path.normpath(v)))
            # APPDATA=...\\AppData\\Roaming，其父 ...\\AppData 覆盖 Roaming/Local/LocalLow 整棵子树
            if ev == "APPDATA":
                dirs.add(os.path.normcase(os.path.normpath(os.path.dirname(v))))
    # 用户名无关兜底：从 USERPROFILE 推导 AppData 子树（极少数无 APPDATA 环境变量时）
    up = os.environ.get("USERPROFILE")
    if up:
        dirs.add(os.path.normcase(os.path.normpath(os.path.join(up, "AppData"))))
    # 硬编码兜底（极少数无环境变量场景）
    for d in ("C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)",
              "C:\\ProgramData", "C:\\Boot", "C:\\EFI", "C:\\Recovery"):
        dirs.add(os.path.normcase(os.path.normpath(d)))
    return dirs


_PROTECTED_SYSTEM_DIRS = _build_protected_system_dirs()


def _build_protected_exact_roots():
    """精确匹配语义的保护根：只保护该目录本身，不保护其子目录
    （子目录是用户正常数据区，如文档/桌面下的文件夹）。"""
    roots = set()
    up = os.environ.get("USERPROFILE")
    if up:
        roots.add(os.path.normcase(os.path.normpath(up)))
    return roots


_PROTECTED_EXACT_ROOTS = _build_protected_exact_roots()


def _subst_real(path):
    """把 subst 虚拟盘符路径解析为真实路径（os.path.realpath 不解析 subst）。
    非 subst 盘（普通盘符返回 \\Device\\...）或失败时返回 None。"""
    drive, rest = os.path.splitdrive(path)
    if not drive or drive.startswith("\\\\"):
        return None
    try:
        buf = ctypes.create_unicode_buffer(65536)
        n = ctypes.windll.kernel32.QueryDosDeviceW(drive[:-1], buf, 65536)
        if n <= 0:
            return None
        target = buf.value
        if not target.startswith("\\??\\"):
            return None
        return target[4:] + rest
    except Exception:
        return None


def _is_readonly(path):
    """文件是否带只读属性（用户刻意的防删标记）。"""
    try:
        return not (os.stat(path).st_mode & stat.S_IWRITE)
    except OSError:
        return False


def _is_protected_system_dir(path):
    """判断 path 是否为系统关键目录（或其任意子目录）。
    受保护集合见 _PROTECTED_SYSTEM_DIRS（Windows / Program Files / ProgramData / AppData 等）。
    先做字符串前缀匹配，再对 realpath（解析 junction/符号链接后的真实目标）判一次，
    防止 D:\\x -> C:\\Windows 这类链接绕过纯字符串匹配。"""
    if not path:
        return False
    p = os.path.normcase(os.path.normpath(path))
    if p in _PROTECTED_EXACT_ROOTS:
        return True
    for d in _PROTECTED_SYSTEM_DIRS:
        if p == d or p.startswith(d + os.sep):
            return True
    # subst 虚拟盘（realpath 不解析）：X: -> C:\Windows 时 X:\System32 也要拦
    sp = _subst_real(path)
    if sp:
        sp = os.path.normcase(os.path.normpath(sp))
        if sp in _PROTECTED_EXACT_ROOTS:
            return True
        for d in _PROTECTED_SYSTEM_DIRS:
            if sp == d or sp.startswith(d + os.sep):
                return True
    try:
        rp = os.path.normcase(os.path.normpath(os.path.realpath(path)))
    except Exception:
        return False
    if rp == p:
        return False
    for d in _PROTECTED_SYSTEM_DIRS:
        if rp == d or rp.startswith(d + os.sep):
            return True
    return False
