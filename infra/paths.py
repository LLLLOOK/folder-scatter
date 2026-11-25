import os
import sys


# 是否打包态：PyInstaller 会设 sys.frozen；Nuitka 不设，但编译过的模块里有
# __compiled__，单文件模式还会带 NUITKA_ONEFILE_PARENT 环境变量。三选一即可，
# 且 pytest 下三者都不成立，不会污染测试的路径基线。
IS_FROZEN = (bool(getattr(sys, "frozen", False))
             or "__compiled__" in globals()
             or "NUITKA_ONEFILE_PARENT" in os.environ)

# 数据目录：撤回历史 / 日志都落这里。开发态 = 项目根；打包态固定到
# %APPDATA%\文件夹散开助手\，让 exe 所在文件夹只放一个 exe，拷给别人也干净，
# 且不会因为把 exe 放进只读目录而写失败。目录建不出来时回退到 exe 同目录，
# 保证不会因此闪退。
if IS_FROZEN:
    _appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    _data_dir = os.path.join(_appdata, "文件夹散开助手")
    try:
        os.makedirs(_data_dir, exist_ok=True)
        _undo_base = _data_dir
    except Exception:
        _undo_base = os.environ.get("NUITKA_ONEFILE_DIRECTORY") or os.path.dirname(
            os.path.abspath(sys.argv[0]))
else:
    _undo_base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 资源目录：随 exe 分发的图片。PyInstaller 单文件在 _MEIPASS；Nuitka 单文件
# 在临时解压目录（sys.executable 同级）；开发态在项目根。
RESOURCE_DIR = (getattr(sys, "_MEIPASS", None)
                or (os.path.dirname(os.path.abspath(sys.executable))
                    if IS_FROZEN else _undo_base))


UNDO_HISTORY_FILE = os.path.join(_undo_base, "undo_history.json")


# GUI 未开时，右键散开的撤回批次先暂存到 pending 文件，主程序启动再合并进主文件
UNDO_PENDING_FILE = os.path.join(_undo_base, "undo_history_pending.json")


# B1 增量账本：散开进行中每 20 条/每个源文件夹边界把已产出撤回记录增量落盘到此，
# 进程崩溃/断电后下次启动自愈合成批次入栈；批次安全落盘后删除
UNDO_INFLIGHT_FILE = os.path.join(_undo_base, "undo_history.inflight.json")


# 撤回历史最多保留的批次数：只留最近 N 批，防止 JSON 无限膨胀拖慢读写
UNDO_HISTORY_MAX_BATCHES = 20


# 右键多选分组文件：Explorer 多选会为每个文件夹各起一个进程（几乎同时启动），
# 同批进程经此文件加入同一组，GUI 把同组的撤回记录合并为「一次操作 = 一批撤回」
UNDO_GROUP_FILE = os.path.join(_undo_base, "undo_group.json")


# 未捕获异常留痕文件：pythonw 无控制台，闪退无任何输出；hook 把 traceback 写到这里，
# 下次启动在日志区展示原因（用户不看控制台，诊断必须进 UI）
CRASH_LOG = os.path.join(_undo_base, "crash.log")


# 覆盖备份目录名（旧布局，兼容历史撤回记录）：目标父目录下的隐藏目录
BACKUP_DIR_NAME = ".scatter_backup"


# 新布局备份根目录名：每个数据盘根下的可见目录（如 D:\散开备份），
# 内按「批次子目录」存放该次操作被覆盖的旧文件；同盘 rename 瞬时不复制
BACKUP_ROOT_NAME = "散开备份"


# 本地进程间通信通道名：静默散开进程（右键）通过它把日志 / 撤回记录 / 父目录同步给正在运行的 GUI 主程序。
# 环境变量可覆盖：pytest 用独立名字（tests/conftest.py 设置），否则测试会连上
# 用户正在运行的 GUI，把假撤回批次写进真实 undo_history.json
IPC_NAME = os.environ.get("SCATTER_IPC_NAME", "FolderScatterIPC")
