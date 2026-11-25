"""App 拆分子模块：窗口生命周期 / 系统托盘 / 几何记忆（方法自 ui/main_window.py 逐字搬移，行为冻结）。

LifecycleMixin 无任何 __init__，全部方法仍写 self（经 App 多继承解析），调用面零改动。
"""

from PyQt6.QtWidgets import (
    QApplication,
    QMenu,
    QSystemTrayIcon,
)

from PyQt6.QtCore import (
    QSize,
    Qt,
)

from ui.icons import _load_app_icon


class LifecycleMixin:
    def _default_size(self):
        """固定默认窗口尺寸为 760x780，不再随屏幕分辨率缩放。"""
        return QSize(760, 780)

    def _center_default(self):
        """将窗口设为默认尺寸 760x780 并居中（用于首次启动与「重置窗口」）。"""
        size = self._default_size()
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            self.resize(size)
            return
        sg = screen.geometry()
        x = sg.x() + (sg.width() - size.width()) // 2
        y = sg.y() + (sg.height() - size.height()) // 2
        self.setGeometry(x, y, size.width(), size.height())

    def _restore_geometry(self):
        """恢复上次关闭时的窗口位置与大小；记录缺失或窗口落在所有屏幕之外时，
        改用默认尺寸 760x780 并居中。"""
        val = self.settings.value("window_geometry")
        if val:
            try:
                parts = [int(v) for v in val.split(",")]
                if len(parts) >= 4:
                    self.setGeometry(parts[0], parts[1], parts[2], parts[3])
                    # 恢复出的窗口完全不在任何屏幕内（如换到更小的屏），回退默认尺寸并居中
                    if not any(self.geometry().intersects(sc.geometry())
                               for sc in QApplication.screens()):
                        self._center_default()
                    return
            except Exception:
                pass
        self._center_default()

    def closeEvent(self, event):
        """关闭窗口时保存当前位置与大小（完整几何）及记忆类设置。
        托盘「退出」菜单会置 _quitting 强制退出；否则按 close_to_tray
        决定隐藏到托盘或彻底退出。"""
        # 托盘「退出」菜单：忽略托盘最小化设置，强制退出
        if getattr(self, "_quitting", False):
            event.accept()
            self._ipc_shutdown()
            self._flush_deferred_undo()
            self._shutdown_workers()
            self._persist_worker_batch_on_exit()
            # 不用 sys.exit(0)：SystemExit 在 Qt virtual 内抛出会被 SIP 路由到
            # 自定义 excepthook 写假 crash.log（M1），且跳过 Qt 正常析构
            QApplication.instance().quit()
            return
        try:
            g = self.geometry()
            # 记忆完整几何（位置 + 大小），使自由缩放后的窗口下次打开保持同样尺寸
            self.settings.setValue(
                "window_geometry",
                f"{g.x()},{g.y()},{g.width()},{g.height()}"
            )
            self._persist_memory_settings()  # 持久化父目录 / 操作配置记忆
        except Exception:
            pass
        # 托盘初始化失败（无系统托盘环境）时不隐藏，否则窗口消失且无托盘入口 → 僵尸进程
        if self.tray is not None and bool(self.settings.value("close_to_tray", False, type=bool)):
            event.ignore()   # 不真正销毁窗口
            self.hide()      # 隐藏到托盘，后台线程与托盘图标保持在线
            return
        event.accept()
        self._ipc_shutdown()
        self._flush_deferred_undo()
        self._shutdown_workers()
        self._persist_worker_batch_on_exit()
        # 未开启托盘最小化 -> 彻底退出（同上方说明：自然退出，不写假 crash.log）
        QApplication.instance().quit()

    # ---------- 系统托盘 ----------
    def _init_tray(self):
        """创建始终驻留的系统托盘图标，提供右键菜单与双击唤出。
        关闭最小化到托盘、静默启动均依赖它。"""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(_load_app_icon())
        self.tray.setToolTip("文件夹散开助手")
        menu = QMenu(self)
        # 去掉原生窗口边框与阴影，配合 QSS 实现真正干净的圆角菜单
        menu.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        menu.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint)
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        act_show = menu.addAction("显示主界面")
        act_show.triggered.connect(self._tray_show)
        act_quit = menu.addAction("退出")
        act_quit.triggered.connect(self._tray_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_activated(self, reason):
        """双击或单击托盘图标唤出主界面。"""
        if reason in (QSystemTrayIcon.ActivationReason.DoubleClick,
                      QSystemTrayIcon.ActivationReason.Trigger):
            self._tray_show()

    def _tray_show(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _ipc_shutdown(self):
        """退出前关停 IPC：防抖窗内已收的右键 done 批次先落栈（M2），
        不再只靠静默端 pending 兜底；必须在 _flush_deferred_undo 之前调用
        （忙碌延后的批次随后由它统一落盘）。"""
        ipc = getattr(self, "ipc", None)
        if ipc is not None:
            ipc.shutdown()

    def _flush_deferred_undo(self):
        """退出前兜底：延后批次直接落栈并持久化（薄壳，实现在 UndoHistoryStore）。"""
        self.undo_store.flush_deferred()

    def _persist_worker_batch_on_exit(self):
        """退出兜底：本地散开未完成批次直接落栈（薄壳，实现在 UndoHistoryStore）。"""
        self.undo_store.persist_worker_batch(self.worker)

    def _shutdown_workers(self):
        """退出前安全停止后台线程：先请求停止、再有限等待其结束。避免散开
        进行中被强杀留下半移动文件，或 C++ 线程在解释器拆卸后回调 Python
        导致崩溃。幂等——重复调用时线程已停会直接跳过。"""
        for attr in ("worker", "scan_worker", "undo_worker"):
            w = getattr(self, attr, None)
            if w is None:
                continue
            try:
                if w.isRunning():
                    if hasattr(w, "request_stop"):
                        w.request_stop()  # 散开循环在下一个文件边界安全停下
                    elif hasattr(w, "abort"):
                        w.abort()         # TreeScanWorker 用 abort 标记（无事件循环，quit 无效）
                    w.wait(5000)          # 最多等 5s，防止卡死退出
            except Exception:
                pass
        # 退役扫描线程同样在跑：退出前也得停下，否则进程拆卸时销毁运行中线程 → 崩溃
        for w in list(getattr(self, "_retired_scans", None) or []):
            try:
                if w.isRunning():
                    w.abort()
                    w.wait(5000)
            except Exception:
                pass
        self._retired_scans = []

    def _tray_quit(self):
        """托盘菜单「退出」：忽略托盘最小化设置，强制彻底退出。"""
        self._quitting = True  # 告知 closeEvent 不要拦截为「最小化到托盘」
        try:
            # app.quit() 不会触发 closeEvent：几何与记忆设置必须在此手动落盘，
            # 否则托盘退出会丢掉本次窗口位置/尺寸变更
            g = self.geometry()
            self.settings.setValue(
                "window_geometry",
                f"{g.x()},{g.y()},{g.width()},{g.height()}"
            )
            self._persist_memory_settings()
        except Exception:
            pass
        self._ipc_shutdown()
        self._flush_deferred_undo()
        self._shutdown_workers()
        self._persist_worker_batch_on_exit()
        QApplication.instance().quit()
