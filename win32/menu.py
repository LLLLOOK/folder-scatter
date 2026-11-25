import os
import sys

from infra.paths import IS_FROZEN, RESOURCE_DIR

try:
    import winreg
except ImportError:  # 非 Windows 平台（如交叉调试）保持可导入
    winreg = None


# ---------- 右键菜单设置（设置窗口 + winreg 直写注册表） ----------

def _find_pythonw():
    """定位 pythonw.exe（无控制台窗口的 Python），供右键菜单静默执行使用。
    优先用当前解释器同目录的 pythonw.exe；找不到则回退 sys.executable（会有控制台）。"""
    d = os.path.dirname(sys.executable)
    cand = os.path.join(d, "pythonw.exe")
    if os.path.exists(cand):
        return cand
    return sys.executable


def _reg_delete_tree(root_path):
    """删除 HKCU 下的键树（含全部子键）。键不存在则静默跳过。"""
    if winreg is None:
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root_path, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _reg_delete_tree(root_path + "\\" + sub)
    except OSError:
        return
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, root_path)
    except OSError:
        pass


# 菜单注册版本号：菜单项文案/命令有变更时递增，保存设置时据此强制重写注册表，
# 老用户即使不动菜单勾选也能刷新到最新菜单名
MENU_REG_VERSION = "2"


def apply_menu_registry(opts):
    """直接用 winreg 写右键菜单注册表（HKCU\\Software\\Classes\\Directory\\shell）。
    opts: {"current": bool, "sub": bool, "all": bool, "undo": bool}
    勾选项 -> 注册；未勾选 -> 整棵删除（全不勾选即干净卸载）。

    替代旧的「生成 menu_config.bat + cmd 隐藏运行」方案：不再经过 cmd 解析，
    彻底消除 bat 注入面（%x% 变量展开）与 GBK errors=replace 的路径损坏，
    且同步完成（不像 ShellExecuteW 发出去就不管结果）。
    命令前缀：打包态（frozen）指向 exe 本身；开发态 pythonw + main.py。"""
    if winreg is None:
        raise RuntimeError("当前平台不支持 winreg，无法注册右键菜单")
    script_dir = RESOURCE_DIR if IS_FROZEN else os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    if IS_FROZEN:
        # 单文件模式下 sys.executable 指向临时解压目录，sys.argv[0] 才是原始 exe；
        # 注册表图标直接用 exe 内嵌图标（,0），不指向会被清理的临时目录。
        prog = '"%s"' % os.path.abspath(sys.argv[0])
        icon_path = os.path.abspath(sys.argv[0]) + ",0"
    else:
        prog = '"%s" "%s"' % (_find_pythonw(), os.path.join(script_dir, "main.py"))
        icon_path = os.path.join(script_dir, "assets", "icons", "appicon.ico")
    reg_root = r"Software\Classes\Directory\shell"
    # (选项键, 注册表子键, 菜单显示文字, 注册表命令)
    entries = [
        ("current", "FolderScatterCurrent", "仅当前层散开",
         '%s --silent-folder "%%1"' % prog),
        ("sub", "FolderScatterSub", "包含子文件夹散开",
         '%s --silent-folder "%%1" --level two' % prog),
        ("all", "FolderScatterAll", "全部层级散开",
         '%s --silent-folder "%%1" --level all' % prog),
        ("undo", "FolderScatterUndo", "在软件中打开（可撤回）",
         '%s "%%1"' % prog),
    ]
    # 分组分割线落点：注册表 shell 子键按名称字母序显示，取所有勾选项里
    # 字母序最小/最大者为分组视觉首/尾，使本软件功能成为独立区块。
    checked_subkeys = [sk for (k, sk, _l, _c) in entries if opts.get(k, False)]
    sep_before = min(checked_subkeys) if checked_subkeys else None
    sep_after = max(checked_subkeys) if checked_subkeys else None
    for key, subkey, label, cmd in entries:
        key_path = reg_root + "\\" + subkey
        # 先整棵删除（干净卸载 / 幂等覆盖，保证状态与勾选完全一致）
        _reg_delete_tree(key_path)
        if not opts.get(key, False):
            continue
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, label)
            # 菜单项前显示的软件图标（绝对路径）
            winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, icon_path)
            # 多选支持：MultiSelectModel=Player 使本命令在“选中多个文件夹”时也出现。
            # 实测（2026-08-14 silent.log）：多选时 Explorer 为每个选中文件夹各起
            # 一个进程、各带自己的 %1，并发冲突靠互斥锁排队 + 撤回分组吸收。
            winreg.SetValueEx(k, "MultiSelectModel", 0, winreg.REG_SZ, "Player")
            if subkey == sep_before:
                winreg.SetValueEx(k, "SeparatorBefore", 0, winreg.REG_SZ, "")
            if subkey == sep_after:
                winreg.SetValueEx(k, "SeparatorAfter", 0, winreg.REG_SZ, "")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path + "\\command", 0,
                                winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, cmd)


# 开机自启注册表项：软件名作为键值；HKCU 无需管理员权限
AUTOSTART_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


AUTOSTART_APP_NAME = "FolderScatter"


def _get_launch_command():
    """返回开机自启应写入注册表的启动命令。
    打包态（frozen）：直接是 .exe 绝对路径；
    开发态：pythonw.exe + 当前脚本绝对路径（这样才能正确启动本软件而非裸解释器）。
    路径均带引号，兼容含空格/中文。"""
    if IS_FROZEN:
        return '"%s"' % os.path.abspath(sys.argv[0])
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
    # 与右键菜单同款用 pythonw（无控制台）：用 sys.executable 若是 python.exe，
    # 开机自启会弹一个控制台黑窗（L2）
    return '"%s" "%s"' % (_find_pythonw(), script)


def _set_autostart(enable):
    """开机自启：enable=True 写入 HKCU\\...\\Run；False 删除该项。
    失败静默（不影响主流程，如注册表被组策略锁定）。"""
    if winreg is None:
        return
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, AUTOSTART_REG_KEY, 0,
            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE)
        try:
            if enable:
                winreg.SetValueEx(
                    key, AUTOSTART_APP_NAME, 0,
                    winreg.REG_SZ, _get_launch_command())
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_APP_NAME)
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
    except Exception:
        pass
