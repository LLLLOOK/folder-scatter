# main.py —— 文件夹散开助手（重构版入口）
#
# 模块分层：infra → core/backup/win32 → undo → ui → main（详见 agents.md 模块地图）。
# 本文件只承载两段入口逻辑：
#   1) run_silent_folder：右键菜单 --silent-folder 静默散开（无 UI，经 IPC 与 GUI 同步）
#   2) __main__：命令行解析、静默/GUI 两种启动路径
#
# 仅提供源码，不打包。


import os
import sys
import time
import json
import uuid
import ctypes

from PyQt6.QtWidgets import QApplication, QMessageBox

from PyQt6.QtCore import QLibraryInfo, QSettings, QTranslator

from PyQt6.QtNetwork import QLocalSocket
from infra.logutil import _append_utf8_bom, _install_crash_hooks, _trim_log_if_big
from infra.paths import BACKUP_DIR_NAME, IPC_NAME, IS_FROZEN, _undo_base
from core.guards import _is_link_or_junction, _is_protected_system_dir
from core.locking import _ScatterBusyError, _acquire_scatter_lock, _release_scatter_lock
from core.scatter import execute_scatter
from undo.store import _append_undo_pending, _clear_inflight, _scatter_group_id
from backup.vault import _new_backup_batch
from ui.icons import _load_app_icon
from ui.main_window import App
from ui.style import _build_stylesheet
from ui.toast import ToastNotify, _LIVE_TOASTS


# ---------- 静默模式（右键后台执行，无 UI） ----------

def _write_silent_log(path, msg):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    _append_utf8_bom(path, f"[{stamp}] {msg}\n")


def _validate_silent_target(folder):
    """散开目标前置防线（目录存在 / 非盘根 / 父目录可用），
    返回 (folder, parent_dir)；不合法直接抛异常。"""
    folder = os.path.normpath(folder)
    if not os.path.isdir(folder):
        raise NotADirectoryError("不是有效文件夹：" + folder)
    # 磁盘根目录防线：根目录的父目录是其自身，散开会变成「同名自我覆盖」导致文件被删
    _drive, _rest = os.path.splitdrive(folder)
    if _drive and _rest in ("", "\\", "/"):
        raise PermissionError("拒绝操作：不能散开磁盘根目录")
    parent_dir = os.path.dirname(folder)
    if not parent_dir or not os.path.isdir(parent_dir):
        raise NotADirectoryError("目标文件夹没有可用的父目录，无法散开：" + folder)
    return folder, parent_dir


def _collect_silent_sources(folder, level):
    """按 level 收集待散开源文件夹列表（含 junction 根降级与备份目录排除）。"""
    # 右键的文件夹本身是链接/junction：two/all 的展开（listdir/os.walk）会穿透
    # 到链接目标的真实目录搬走别人的内容——退化为仅处理本体，
    # 由 promote 入口的既有拦截跳过并留日志
    if level != "one" and _is_link_or_junction(folder):
        level = "one"
    if level == "all":
        # 全部层级：递归收集 folder 下所有层级的子文件夹
        # 注意：os.walk 的 dirs 已是“子目录”，不含 root 自身，无需跳过首层
        sub_folders = []
        for root, dirs, files in os.walk(folder):
            # 原地过滤链接/junction：os.walk 对 junction 不受 followlinks=False 约束，
            # 会穿透到目标位置；过滤后既不递归下去，也不纳入散开列表
            dirs[:] = [d for d in dirs
                       if d != BACKUP_DIR_NAME
                       and not _is_link_or_junction(os.path.join(root, d))]
            for d in dirs:
                sub_folders.append(os.path.join(root, d))
        # folder 本身 + 所有子文件夹；execute_scatter 按深度倒序处理 → 展平到父目录
        return [folder] + sub_folders
    if level == "two":
        # 包含子文件夹：folder 本身 + 其直接子文件夹
        # execute_scatter 按深度倒序处理：先散直接子文件夹（内容上提到 folder），
        # 再散 folder（此时已含上提内容）→ 一并提到父目录，实现“两层”效果
        selected = [folder]
        try:
            for d in os.listdir(folder):
                if d == BACKUP_DIR_NAME:
                    continue  # 备份目录不参与散开
                sub = os.path.join(folder, d)
                if os.path.isdir(sub):
                    selected.append(sub)
        except OSError:
            pass
        return selected
    # 仅当前层：只散 folder 这一层（文件 + 子文件夹整体）到父目录
    return [folder]


def _load_silent_prefs():
    """读右键菜单默认操作方式 / 冲突处理 + 忽略隐藏 + 忽略体积偏好。
    返回 (menu_mode, menu_conflict, skip_hidden, max_size, to_trash)，异常回退默认。"""
    try:
        qs = QSettings("FolderPromoter", "FolderPromoter")
        menu_mode = str(qs.value("menu_mode", "move"))
        if menu_mode not in ("move", "copy"):
            menu_mode = "move"
        menu_conflict = str(qs.value("menu_conflict", "overwrite"))
        if menu_conflict not in ("overwrite", "skip", "rename"):
            menu_conflict = "overwrite"
        skip_hidden = bool(qs.value("ignore_hidden", False, type=bool))
        ignore_checked = bool(qs.value("ignore_checked", False, type=bool))
        ignore_size = int(qs.value("ignore_size", 500))
        to_trash = bool(qs.value("conflict_trash", True, type=bool))
    except Exception:
        menu_mode = "move"
        menu_conflict = "overwrite"
        skip_hidden = False
        ignore_checked = False
        ignore_size = 500
        to_trash = True
    max_size = ignore_size if ignore_checked else 0
    return menu_mode, menu_conflict, skip_hidden, max_size, to_trash


def _show_silent_start_toast():
    """开始提示 Toast：散开是同步阻塞流程，不先泵事件 Toast 永远画不出来
    （淡出定时器也依赖事件循环），长任务全程零反馈。先泵一小会儿让它上屏
    常亮，结束后由 __main__ 的收尾泵循环走淡出。"""
    try:
        ToastNotify("后台散开任务已启动。").show()
        _app = QApplication.instance()
        if _app is not None:
            _app.processEvents()
            time.sleep(0.3)
            _app.processEvents()
    except Exception:
        pass


class _SilentIpc:
    """右键进程 → GUI 的本地 IPC 通道：日志实时同步 + done 批次同步（A4 ack/重投）。
    连接失败（软件没开 / 多开时本进程非监听方）则静默降级，仅写文件。"""

    def __init__(self):
        self.sock = None
        self.connected = False
        try:
            if QApplication.instance() is not None:
                self.sock = QLocalSocket()
                self.sock.connectToServer(IPC_NAME)
                self.connected = self.sock.waitForConnected(500)
        except Exception:
            self.connected = False

    def send(self, obj):
        if not self.connected or self.sock is None:
            return
        try:
            line = json.dumps(obj, ensure_ascii=False) + "\n"
            self.sock.write(line.encode("utf-8"))
            # GUI 中途崩溃（socket 断开）时 write 可能不抛异常但发不出去：
            # waitForBytesWritten 失败必须复位标志，否则 done 后不写 pending，
            # 撤回记录两头丢（GUI 收不到 done、pending 又没写）——2026-08-27 实发
            if not self.sock.waitForBytesWritten(1000):
                self.connected = False
        except Exception:
            self.connected = False

    @staticmethod
    def _try_sync_once(sock, done_msg, timeout_ms):
        """在已建立的 sock 上发 done 并等 ack；任一步失败返回 False。"""
        try:
            line = json.dumps(done_msg, ensure_ascii=False) + "\n"
            sock.write(line.encode("utf-8"))
            if not sock.waitForBytesWritten(timeout_ms):
                return False
            return bool(sock.waitForReadyRead(timeout_ms)
                        and b"ack" in bytes(sock.readAll()))
        except Exception:
            return False

    def sync_done(self, done_msg, has_records):
        """done 已在主连接上发出，这里等 GUI 落盘后的 ack（A4 应用层 ack：
        GUI「已收字节、未落盘」之间崩溃会丢记录且双方都不知情）。
        没等到 ack 换全新连接整轮重发（最多 2 次）；重复投递由 GUI 按
        batch_id 幂等去重，无副作用。无记录可同步时视为已成功（不空等）。"""
        synced = not has_records
        if not synced and self.connected and self.sock is not None:
            try:
                synced = bool(self.sock.waitForReadyRead(3000)
                              and b"ack" in bytes(self.sock.readAll()))
            except Exception:
                synced = False
            for _ in range(2):
                if synced:
                    break
                _s = QLocalSocket()
                try:
                    _s.connectToServer(IPC_NAME)
                    if _s.waitForConnected(1500):
                        synced = self._try_sync_once(_s, done_msg, 1500)
                except Exception:
                    synced = False
                finally:
                    try:
                        _s.abort()  # 先断开再丢引用，防 sip 对象悬空（agents.md 约定）
                    except Exception:
                        pass
        return synced

    def close(self):
        if self.connected and self.sock is not None:
            try:
                self.sock.disconnectFromServer()
                self.sock.waitForDisconnected(500)
            except Exception:
                pass


def _stash_pending_undo(silent_undo, group_id, errlog):
    """GUI 未开 / 发不出去 / 没等到 ack 时，把撤回批次暂存到 pending 文件，
    主程序下次启动合并进 undo_history.json（重复落盘由启动合并按内容去重，A4）。
    返回是否写入成功。"""
    if not silent_undo:
        return False
    # pending 是读-改-写：多进程并发会互相覆盖丢批次，用散开互斥锁串行化；
    # 抢不到锁绝不无锁写（A2：无锁写会互覆其他进程的暂存/主历史），
    # 记错误日志留痕——宁可丢失有痕，不可互覆无痕
    _pend_ok, _pend_reason = _acquire_scatter_lock(600000)
    if _pend_ok:
        try:
            _append_undo_pending(silent_undo, group_id)
            return True
        finally:
            _release_scatter_lock()
    _write_silent_log(errlog, f"[错误] 撤回暂存未能写入（{_pend_reason}），"
                              "本批撤回记录无法保留")
    return False


def run_silent_folder(folder, level="one", lock_wait_ms=0):
    """被 --silent-folder 调用：把右键选中的 folder 散开到它的父目录。
    level:
      "one" 仅当前层 —— 把 folder 的直接内容（文件 + 子文件夹整体）上移一层到父目录；
      "two" 包含子文件夹 —— folder 的直接子文件夹也参与散开（连同其内文件一起提到父目录）；
      "all" 全部层级 —— 递归把 folder 内所有层级的文件展平到父目录。
    lock_wait_ms: 获取互斥锁的最长等待毫秒数（多选时 Explorer 会起多个进程，
    排队等锁让各文件夹顺序散开；0 = 不等待，抢不到立即失败）。
    返回 (移动文件数, 处理文件夹数)。"""
    folder, parent_dir = _validate_silent_target(folder)
    # 分组 id：多选时 Explorer 为每个文件夹各起一个进程，同批进程共享同一组 id，
    # GUI 据此把 N 个文件夹的撤回记录合并为「一次右键操作 = 一批撤回」
    _group_id, _group_created = _scatter_group_id(parent_dir)
    # A4：本批撤回记录的一次性批次 id（重发/pending 复用同一 id），
    # GUI 按它幂等去重——没等到 ack 重投同批不会被压栈两次
    _batch_id = uuid.uuid4().hex

    # 开发态沿用 __file__（测试靠 monkeypatch 它把日志隔离到临时目录）；
    # 打包态 __file__ 指向编译时的源码路径，必须切到统一的数据目录。
    log_dir = _undo_base if IS_FROZEN else os.path.dirname(os.path.abspath(__file__))
    errlog = os.path.join(log_dir, "error.log")
    silent_log = os.path.join(log_dir, "silent.log")

    ipc = _SilentIpc()
    # 桌面提示一次操作只弹一次：建组进程且 GUI 未连接才弹开始提示
    # （GUI 连着时由 GUI 在完成后弹一次）；多选不再叠 N 个提示
    if _group_created and not ipc.connected:
        _show_silent_start_toast()

    def s_log(msg):
        # 错误单独进 error.log，方便快速排查；所有日志都进 silent.log
        if msg.startswith("[错误]"):
            _write_silent_log(errlog, msg)
        _write_silent_log(silent_log, msg)
        ipc.send({"type": "log", "msg": msg})

    # 系统关键目录保护：禁止散开系统目录或其父目录
    # （如右键 C:\Windows\System32 会把内容提到 C:\Windows，导致系统崩溃）
    if _is_protected_system_dir(folder) or _is_protected_system_dir(parent_dir):
        _write_silent_log(errlog,
                          f"[错误] 拒绝散开系统关键目录：{folder}（父目录 {parent_dir}）")
        raise PermissionError("拒绝操作：系统关键目录，禁止散开")

    selected = _collect_silent_sources(folder, level)
    _write_silent_log(silent_log, f"[调试] 收到文件夹路径: {folder}")
    _write_silent_log(silent_log, f"[调试] 父目录(目标位置): {parent_dir}")
    _write_silent_log(silent_log, f"[调试] 模式: {level}  待散开源({len(selected)}): "
                                  f"{[os.path.basename(s) for s in selected][:50]}")
    menu_mode, menu_conflict, skip_hidden, max_size, to_trash = _load_silent_prefs()

    # 静默模式无 UI，skip 回调只累计计数；总结由 execute_scatter 统一输出（含跳过/占用/权限统计）
    silent_skip = [0, 0, 0]

    def s_skip(skipped, occupied, permission):
        silent_skip[0] = skipped
        silent_skip[1] = occupied
        silent_skip[2] = permission

    silent_undo = []  # 收集本次散开的撤回记录，供 GUI 主程序同步撤回
    moved, folders = execute_scatter(selected, parent_dir, menu_mode, menu_conflict,
                                     None, max_size, False, s_log,
                                     lambda n: None, s_skip, lambda: False, silent_undo,
                                     skip_hidden=skip_hidden, to_trash=to_trash,
                                     lock_wait_ms=lock_wait_ms,
                                     backup_batch=_new_backup_batch())
    # 通知主程序本次任务完成（父目录 + 撤回记录），供 GUI 实时同步显示与撤回
    _done_msg = {"type": "done", "parent_dir": parent_dir, "undo_records": silent_undo,
                 "group": _group_id, "batch_id": _batch_id}
    ipc.send(_done_msg)
    _synced = ipc.sync_done(_done_msg, bool(silent_undo))

    _pend_written = _stash_pending_undo(silent_undo, _group_id, errlog) \
        if silent_undo and not _synced else False
    # B1：撤回记录已安全转移（GUI ack / pending 暂存 / 本无记录）才清 inflight
    # 增量账本；转移失败保留——下次 GUI 启动自愈兜底，不丢在途记录
    if _synced or _pend_written or not silent_undo:
        _clear_inflight()
    ipc.close()
    return moved, folders


if __name__ == "__main__":
    _install_crash_hooks()  # 闪退留痕：未捕获异常写 crash.log，下次启动展示
    # ---------- 命令行参数检测：右键菜单静默后台执行 ----------
    args = sys.argv[1:]
    silent_folders = []   # 多选：所有选中文件夹（逐个散开）
    silent_level = "one"
    if "--silent-folder" in args:
        i = args.index("--silent-folder")
        # 实测：多选时 Explorer 每文件夹各起一进程、各带自己的路径；
        # 这里取 --level 之前的全部路径仅为兼容命令行手传多个目录的写法
        j = args.index("--level") if "--level" in args else len(args)
        silent_folders = args[i + 1:j]
        if "--level" in args:
            k = args.index("--level")
            if k + 1 < len(args) and args[k + 1] in ("one", "all", "two"):
                silent_level = args[k + 1]

    def _silent_msg(title, text, icon=0x40):
        """静默模式提示：有 Qt 环境时弹软件同款主题样式的对话框，
        否则回退 Win32 消息框（icon: 0x40 信息 / 0x30 警告 / 0x10 错误）。"""
        try:
            app = QApplication.instance()
            if app is not None:
                if not app.styleSheet():
                    _tm = str(QSettings("FolderPromoter", "FolderPromoter")
                              .value("theme_mode", "dark"))
                    app.setStyleSheet(_build_stylesheet(App._resolve_is_dark(_tm)))
                mb = QMessageBox()
                mb.setWindowTitle(title)
                mb.setText(text)
                mb.setIcon(QMessageBox.Icon.Critical if icon == 0x10
                           else QMessageBox.Icon.Warning if icon == 0x30
                           else QMessageBox.Icon.Information)
                mb.exec()
                return
        except Exception:
            pass
        try:
            ctypes.windll.user32.MessageBoxW(0, text, title, icon)
        except Exception:
            pass

    if "--silent-folder" in args:
        # 参数缺失时直接报错，不要误进入 GUI 模式
        if not silent_folders:
            _silent_msg("文件夹散开工具", "右键菜单调用参数不完整：缺少文件夹路径。\n"
                        "请重新保存右键菜单设置后再试。", 0x10)
            sys.exit(1)

        errlog = os.path.join(_undo_base, "error.log")
        silent_log = os.path.join(_undo_base, "silent.log")

        # 轻量悬浮反馈：用自定义 Toast 替代托盘气泡（不依赖 Windows 通知中心，
        # 关掉通知中心也能立刻显示）。Toast 自行 2.5s 后淡出关闭；进程需让事件
        # 循环持续运转直到它彻底消失，否则退出会掐掉它。
        _sa = None
        try:
            _sa = QApplication(sys.argv[:1])
            # 与 GUI 同款：标准弹窗按钮中文化 + 弹窗带软件图标
            _qt_t = QTranslator(_sa)
            try:
                _qt_t.load("qtbase_zh_CN",
                           QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
                _sa.installTranslator(_qt_t)
            except Exception:
                pass
            _sa.setWindowIcon(_load_app_icon())
            _trim_log_if_big(silent_log)  # 日志超 3MB 自动裁剪到 1MB
            _sa.processEvents()
        except Exception:
            pass

        try:
            # 多选：一个进程内顺序散开所有文件夹，每个发一次 IPC done，
            # 主程序按文件夹逐个刷新（天然错开，不会像 N 个并发进程那样触发刷新风暴）。
            # 失败不逐个弹模态框（会阻塞后续文件夹），收集后循环结束一次性汇总。
            # lock_wait_ms=10min：Explorer 对多选会为每个文件夹各起一个进程，
            # 排队等互斥锁即可让各进程顺序散开，而不是互相冲突报「另一个散开任务正在进行」。
            _fail_list = []  # [(文件夹, 原因)]
            for _sf in silent_folders:
                try:
                    run_silent_folder(_sf, silent_level, lock_wait_ms=600000)
                except _ScatterBusyError as e:
                    # 锁冲突（本地/另一右键任务进行中）：原因已由 execute_scatter 写入
                    # silent.log/error.log；这里补一条失败标记供主程序下次启动载入日志框
                    _stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    _append_utf8_bom(silent_log,
                        f"[{_stamp}] [错误] 右键散开失败：{_sf}：{e}\n")
                    _fail_list.append((_sf, str(e)))
                except Exception as e:
                    import traceback
                    _stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    _append_utf8_bom(errlog,
                        f"[{_stamp}] 静默执行失败：{_sf}\n" + traceback.format_exc() + "\n")
                    # 往 silent.log 写一条红色报错，主程序下次启动会把它载入日志框
                    _append_utf8_bom(silent_log,
                        f"[{_stamp}] [错误] 右键散开失败：{_sf}：{e}\n")
                    _fail_list.append((_sf, str(e)))
            if _fail_list:
                # 三行式：标题 / · 路径 / 原因，去掉冗长详情句，弹窗更紧凑
                _lines = "\n".join(f"· {p}\n{r}" for p, r in _fail_list[:6])
                if len(_fail_list) > 6:
                    _lines += f"\n…等共 {len(_fail_list)} 个失败"
                _silent_msg("文件夹散开助手",
                            f"{len(_fail_list)} 个文件夹散开失败：\n{_lines}", 0x10)
            # 通知资源管理器刷新：按唯一父目录逐个 UPDATEDIR
            # （旧的 0x08000000 是 ASSOCCHANGED——文件关联变更，对目录视图无效）
            try:
                _SHCNE_UPDATEDIR = 0x00001000
                _SHCNF_PATHW = 0x0005
                _parents = []
                for _sf in silent_folders:
                    _pd = os.path.dirname(os.path.normpath(_sf))
                    if _pd and _pd not in _parents:
                        _parents.append(_pd)
                for _pd in _parents:
                    ctypes.windll.shell32.SHChangeNotify(
                        _SHCNE_UPDATEDIR, _SHCNF_PATHW, str(_pd), None)
            except Exception:
                pass
        finally:
            # 关键：让事件循环持续运转，直到 Toast 自行淡出关闭（约 2.5s 显示 + 0.4s 淡出），
            # 否则进程退出会掐掉它。散开本身耗时已计入（若 >3s 则不必久等）。
            if _sa is not None and _LIVE_TOASTS:
                end = time.time() + 6.0
                while time.time() < end and \
                        any(t.isVisible() for t in list(_LIVE_TOASTS)):
                    _sa.processEvents()
                    time.sleep(0.05)
            # 0.8s 缓冲让 Windows 完成文件状态刷新，减少图标闪烁。
            time.sleep(0.8)
            sys.exit(0)

    # ---------- 正常 GUI 模式 ----------
    app = QApplication(sys.argv)
    # 加载 Qt 自带简体中文翻译：所有标准弹窗按钮（OK/Cancel 等）自动中文化；
    # 翻译文件缺失（如打包未收录）时静默回退英文，不影响主功能
    _qt_trans = QTranslator(app)
    try:
        _qt_trans.load("qtbase_zh_CN",
                       QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
        app.installTranslator(_qt_trans)
    except Exception:
        pass
    app_icon = _load_app_icon()
    app.setWindowIcon(app_icon)
    window = App()
    window.setWindowIcon(app_icon)
    # 右键菜单以普通路径参数启动（如“撤回”）：预载该文件夹
    if args and os.path.isdir(args[0]):
        window.preload_folder(args[0])
    # 静默启动：隐藏主界面，仅显示托盘（双击托盘图标可唤出）
    # 若系统不支持托盘则回退为正常显示，避免无窗口且无托盘的僵尸进程
    if bool(window.settings.value("silent_start", False, type=bool)) and \
            getattr(window, "tray", None) is not None:
        window.hide()
    else:
        window.show()
    sys.exit(app.exec())
