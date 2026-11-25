"""主窗口 App：装配根（构造 + 各控制器直通属性/薄壳转发）。

业务方法已按面板逐字拆出为 mixin（行为冻结，经多继承解析，调用面零改动）：
- ui/app_lifecycle.py  LifecycleMixin：窗口几何/系统托盘/退出流程
- ui/app_build.py      BuildMixin：    _build_* 界面构建 + DropLineEdit
- ui/app_theme.py      ThemeMixin：    主题解析/切换与局部样式
- ui/app_tree.py       TreeOpsMixin：  目录树勾选/增量刷新/浏览
- ui/app_options.py    OptionsMixin：  选项回调/记忆设置/后缀解析/设置对话框
- ui/app_run.py        RunMixin：      散开执行（开始/模拟/停止）与撤回
- ui/app_events.py     EventsMixin：   事件处理（悬停/拖拽/resize/mousePress）
"""

from PyQt6.QtWidgets import (
    QFileIconProvider,
    QMainWindow,
)

from PyQt6.QtCore import (
    QSettings,
)

from undo.history import UndoHistoryStore
from backup.maintenance import (apply_backup_retention, clean_orphan_backups,
                                collect_backup_dirs, empty_backup_root)
from ui.app_build import BuildMixin
from ui.app_events import EventsMixin
from ui.app_lifecycle import LifecycleMixin
from ui.app_options import OptionsMixin
from ui.app_run import RunMixin
from ui.app_theme import ThemeMixin
from ui.app_tree import TreeOpsMixin
from ui.ipc_server import IpcServer
from ui.log_panel import LogPanel
from ui.scan_controller import ScanController
from ui.taskbar_ctl import TaskbarController


class App(LifecycleMixin, BuildMixin, ThemeMixin, TreeOpsMixin, OptionsMixin,
          RunMixin, EventsMixin, QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("文件夹散开助手")
        self.resize(760, 780)  # 预显示占位尺寸；真正尺寸由 _restore_geometry 按记忆/默认尺寸决定
        self.setAcceptDrops(True)  # 主窗口全局接受拖拽（任意空白区域拖入文件夹）
        self.settings = QSettings("FolderPromoter", "FolderPromoter")
        self.settings.remove("refresh_after_clear")  # 已废弃：清空目录统一为全部清空
        self.theme_mode = str(self.settings.value("theme_mode", "dark"))
        self.is_dark = self._resolve_is_dark(self.theme_mode)
        self.parent_dir = ""
        self.folder_items = []  # 仅文件夹 item
        self.mode = "move"
        self.conflict = "overwrite"
        self.level = "one"
        # 日志区：LogPanel 持有日志控件与着色/导出/历史日志等全部日志逻辑
        # （ui/log_panel.py）。self.log 在 _build_ui 创建后 attach 给它；
        # _log_warn/_log_error/_log_overflow_notified 是它的直通属性（见类内
        # property），各读写点保持原样；status 回调懒解析 status_label（此时未创建）
        self.log_panel = LogPanel(
            status=lambda t: self.status_label.setText(t),
            collect_backup_dirs=self._collect_backup_dirs,
            parent=self)
        self._skip_counts = (0, 0, 0)  # (skipped, occupied, permission) 用于主题切换后重建警示条
        # 散开开始前拍下的目录树勾选快照（按批次对象 id 存放）：撤回该批时用它
        # 还原散开前的勾选布局；_pre_scatter_checks 是本轮散开的暂存快照
        self._check_snapshots = {}
        self._pre_scatter_checks = None
        self.icon_provider = QFileIconProvider()
        # 撤回栈存储：栈的唯一拥有者（undo/history.py，纯 Python 可单测）。
        # undo_stack/_last_undo_group/_deferred_undo 是它的直通属性（见类内 property），
        # 各读写点保持原样；set_undo_enabled 回调转发撤回按钮可用态
        self.undo_store = UndoHistoryStore(
            self.log_msg, lambda b: self.btn_undo.setEnabled(b))
        self.undo_stack = []  # 栈式撤回：每次散开（含右键静默）压入一批记录，撤回时弹栈还原
        self.worker = None
        self.undo_worker = None  # 撤回后台线程（持有 .scatter.lock，结束自释放）
        # 目录树扫描调度：ScanController 持有扫描线程/退役保活列表/代际号
        # （ui/scan_controller.py）。scan_worker/_retired_scans/_pending_summary
        # 是它的直通属性（见类内 property），各读写点保持原样；folder_items
        # 与它共享同一列表对象；树/进度条等控件在 _build_ui 创建后 attach 给它
        self.scan_ctl = ScanController(
            self,
            folder_items=self.folder_items,
            show_root_warning=self._show_root_warning,
            show_system_warning=self._show_system_warning,
            save_parent_dir=self._save_parent_dir,
            update_tree_hint=self._update_tree_hint,
            apply_default_checks=self.apply_default_checks,
            set_parent_dir=lambda p: setattr(self, "parent_dir", p))
        # 任务栏进度条控制：TaskbarController 持有定时器/可用性/忙碌标记
        # （ui/taskbar_ctl.py）。_remote_active/_tb_timer/_taskbar_supported/
        # _tb_local_total 是它的直通属性（见类内 property），各读写点保持原样；
        # hwnd/progress 回调懒解析（构造时 progress 控件尚未创建）
        self.taskbar_ctl = TaskbarController(
            self,
            hwnd=lambda: int(self.winId()),
            progress_value=lambda: self.progress.value())

        self._build_ui()
        self.apply_theme()
        self._restore_geometry()
        # 小屏保护：窗口不低于此兜底下限（内部布局最小尺寸通常更大，此处仅防被拖到极小）
        self.setMinimumSize(400, 380)
        self._restore_memory_settings()  # 恢复记住的父目录 / 操作配置（或写死初始值）
        self._init_tray()                # 系统托盘（开机自启/关闭最小化/静默启动 的前提）
        self._init_ipc()                 # 监听本地 socket，接收右键静默散开的日志/撤回/父目录同步
        # 本地散开进行中收到的右键撤回批次：先挂起，待 _on_worker_finished 再压栈，
        # 避免运行中点亮撤回按钮引发「边散开边撤回」的并发移动（记录本身不丢）
        self._deferred_undo = []
        # 最近一次压栈批次的右键分组 id：同组续批合并进栈顶（一次右键操作=一批撤回）；
        # 本地散开/撤回/启动载入后重置为 None，防止跨操作误合并
        self._last_undo_group = None
        self._show_crash_log()  # 上次闪退原因展示在日志区（若有）

    # ---------- 撤回栈直通属性（UndoHistoryStore 持有真身，读写点保持原样） ----------
    @property
    def undo_stack(self):
        return self.undo_store.stack

    @undo_stack.setter
    def undo_stack(self, value):
        self.undo_store.stack = value

    @property
    def _last_undo_group(self):
        return self.undo_store.last_group

    @_last_undo_group.setter
    def _last_undo_group(self, value):
        self.undo_store.last_group = value

    @property
    def _deferred_undo(self):
        return self.undo_store.deferred

    @_deferred_undo.setter
    def _deferred_undo(self, value):
        self.undo_store.deferred = value

    # ---------- IPC 直通属性（IpcServer 持有真身，外部读写点保持原样） ----------
    @property
    def _ipc_done_timer(self):
        return self.ipc._done_timer

    # ---------- 日志直通属性（LogPanel 持有真身，读写点保持原样） ----------
    # skip_label/root_warning_label/警示条 HTML 复用日志色；触顶标记在
    # _build_ui 初始化与 _on_worker_finished 自动清空日志时重置
    @property
    def _log_warn(self):
        return self.log_panel._log_warn

    @property
    def _log_error(self):
        return self.log_panel._log_error

    @property
    def _log_overflow_notified(self):
        return self.log_panel._log_overflow_notified

    @_log_overflow_notified.setter
    def _log_overflow_notified(self, value):
        self.log_panel._log_overflow_notified = value

    # ---------- 扫描调度直通属性（ScanController 持有真身，读写点保持原样） ----------
    @property
    def scan_worker(self):
        return self.scan_ctl.scan_worker

    @scan_worker.setter
    def scan_worker(self, value):
        self.scan_ctl.scan_worker = value

    @property
    def _retired_scans(self):
        return self.scan_ctl._retired_scans

    @_retired_scans.setter
    def _retired_scans(self, value):
        self.scan_ctl._retired_scans = value

    @property
    def _pending_summary(self):
        return self.scan_ctl._pending_summary

    @_pending_summary.setter
    def _pending_summary(self, value):
        self.scan_ctl._pending_summary = value

    # ---------- 任务栏直通属性（TaskbarController 持有真身，读写点保持原样） ----------
    @property
    def _remote_active(self):
        return self.taskbar_ctl._remote_active

    @property
    def _tb_timer(self):
        return self.taskbar_ctl._timer

    @property
    def _taskbar_supported(self):
        return self.taskbar_ctl.supported

    @property
    def _tb_local_total(self):
        return self.taskbar_ctl._local_total

    @_tb_local_total.setter
    def _tb_local_total(self, value):
        self.taskbar_ctl._local_total = value

    # ---------- 本地 IPC：接收右键静默散开的同步 ----------
    def _init_ipc(self):
        """监听本地 socket（薄壳）：实现整体在 ui/ipc_server.py IpcServer，
        与 App 的交互收窄为构造注入的回调（日志/批次落盘/父目录预载/任务栏进度）。
        若已有实例在监听（用户多开软件），本实例 listen 失败则降级为不监听，不影响自身使用。"""
        self.ipc = IpcServer(
            self,
            log=self.log_msg,
            push_batch=self._push_undo_batch,
            is_local_busy=lambda: self.worker is not None,
            is_remote_active=lambda: self._remote_active,
            taskbar_start=self._taskbar_start,
            taskbar_ping=lambda: self._tb_timer.start(30000),
            taskbar_done=self._taskbar_done,
            is_root_dir=self._is_root_dir,
            current_dir=lambda: self.entry_path.text().strip(),
            set_dir=self.entry_path.setText,
            refresh=self.refresh)

    def _ipc_flush_done(self):
        """右键 done 防抖落盘（薄壳）：tests 确定性触发用；实现见 IpcServer._flush_done。"""
        self.ipc._flush_done()

    # ---------- 任务栏进度条（薄壳，实现见 ui/taskbar_ctl.py TaskbarController） ----------
    def _taskbar_start(self):
        """右键散开进行中：主窗口任务栏按钮显示绿色“忙碌”进度条。"""
        self.taskbar_ctl.start()

    def _taskbar_done(self):
        """右键散开完成：进度条先置 100%，1.5s 后清除。"""
        self.taskbar_ctl.done()

    def _taskbar_local_max(self, total):
        """子线程扫描出文件总数：任务栏从忙碌态转绿色百分比进度。"""
        self.taskbar_ctl.local_max(total)

    def _taskbar_local_prog(self, cur):
        """本地散开进度同步任务栏；总数还没出来时保持忙碌态不重复设置。"""
        self.taskbar_ctl.local_prog(cur)

    def _push_undo_batch(self, recs, group, batch_ids=()):
        """压入一批右键撤回记录（薄壳）：忙碌判定依赖 worker 状态（App 侧），
        落栈/落盘在 UndoHistoryStore.push_batch；返回是否已安全落盘
        （A4：调用方据此决定是否给静默端回 ack）。
        batch_ids：本批携带的 A4 批次 id 集合，供延后队列幂等去重（F28）。"""
        undo_busy = (self.undo_worker is not None and self.undo_worker.isRunning())
        return self.undo_store.push_batch(
            recs, group, busy=(self.worker is not None or undo_busy),
            batch_ids=batch_ids)

    # ---------- 目录树扫描调度（薄壳，实现见 ui/scan_controller.py ScanController） ----------
    def _abort_active_scan(self):
        """中止在途扫描线程并递增代际号（refresh 换扫描 / 无效路径 / 清空目录共用）。"""
        self.scan_ctl._abort_active_scan()

    def refresh(self, preserve_full=False, snapshot=None):
        """扫描并填充目录树（异步、不卡 UI）。preserve_full=True（撤回场景用）：
        snapshot 为散开前的勾选快照（按批次保存），扫描后原样恢复。"""
        self.scan_ctl.refresh(preserve_full, snapshot)

    def _collect_folder_items(self):
        """遍历已填充的目录树，收集所有文件夹 item（folder_items）。"""
        self.scan_ctl._collect_folder_items()

    # ---------- 日志（彩色）（薄壳，实现在 ui/log_panel.py LogPanel） ----------
    def log_msg(self, msg):
        """追加彩色日志（薄壳）：worker/undo_worker/IPC/Store 信号均 connect 本壳，不动。"""
        self.log_panel.log_msg(msg)

    def clear_log(self):
        """清空日志（薄壳，实现在 LogPanel.clear_log）。"""
        self.log_panel.clear_log()

    def copy_errors(self):
        """复制全部 [错误] 行到剪贴板（薄壳，实现在 LogPanel.copy_errors）。"""
        self.log_panel.copy_errors()

    def export_log(self):
        """导出整份日志为 UTF-8(BOM) 文本（薄壳，实现在 LogPanel.export_log）。"""
        self.log_panel.export_log()

    # ---------- 撤回 ----------
    def _save_undo_history(self):
        """将撤回栈持久化到 undo_history.json（薄壳，实现在 UndoHistoryStore.save）。
        返回是否已落盘（A4：IPC 批次只有落盘成功才给静默端回 ack）。"""
        # 顺带清掉已出栈批次的勾选快照：id() 在对象回收后可能被复用，
        # 残留条目会让新批次误领旧快照（20 批截断/撤回出栈都走这里）
        try:
            _alive = {id(b) for b in self.undo_stack}
            for _k in list(self._check_snapshots):
                if _k not in _alive:
                    self._check_snapshots.pop(_k, None)
        except Exception:
            pass
        return self.undo_store.save()

    def _load_undo_history(self):
        """启动时从 undo_history.json 恢复撤回栈（薄壳，实现在 UndoHistoryStore）。"""
        self.undo_store.load_on_startup()

    def _load_history_log(self):
        """启动时载入上次右键散开失败摘要（薄壳，实现在 LogPanel._load_history_log）。"""
        self.log_panel._load_history_log()

    def _show_crash_log(self):
        """启动时展示上次闪退原因（薄壳，实现在 LogPanel._show_crash_log）。"""
        self.log_panel._show_crash_log()

    def _apply_backup_retention(self):
        """按天保留（设置 backup_max_days，0=关闭）：删除超期的记录引用备份。
        被删的覆盖将不可自动还原——日志明确告知。
        （薄壳，实现在 backup.maintenance.apply_backup_retention）"""
        try:
            days = int(self.settings.value("backup_max_days", 0, type=int))
        except (TypeError, ValueError):
            days = 0
        apply_backup_retention(self.undo_store.stack, self.log_msg, days)

    def _clean_orphan_backups(self, roots=None):
        """手动清理失效备份：删除未被当前撤回栈引用的备份文件（淘汰批次残留等），
        再逐级清理空批次目录/根目录。只删备份布局内的文件，返回删除数。
        roots 为 None 时扫描全部备份根目录。
        （薄壳，实现在 backup.maintenance.clean_orphan_backups）"""
        return clean_orphan_backups(self.undo_store.stack, self.log_msg, roots)

    def _empty_backup_root(self, root):
        """显式清空某备份根目录：全部备份文件移入回收站（可找回），
        再逐级清理空批次目录；「散开备份」盘根空了也删。返回移入数。
        （薄壳，实现在 backup.maintenance.empty_backup_root）"""
        return empty_backup_root(self.undo_store.stack, self.log_msg, root)

    def _invalidate_undo_backups(self, root):
        """某备份根被清空后调用：引用这些备份的撤回批次整批移出栈
        （薄壳，实现在 UndoHistoryStore.invalidate_backups）。返回移除的批数。"""
        return self.undo_store.invalidate_backups(root)

    def _sync_backup_notice_log(self):
        """同步日志里的 [备份] 提示（薄壳，实现在 LogPanel._sync_backup_notice_log；
        settings_dialog 调用点零改动）。"""
        self.log_panel._sync_backup_notice_log()

    def _collect_backup_dirs(self):
        """汇总撤回栈仍引用、且文件确实存在的备份目录 -> {目录: 文件数}。
        （薄壳，实现在 backup.maintenance.collect_backup_dirs）"""
        return collect_backup_dirs(self.undo_store.stack, self.log_msg)

    def _show_backup_notice(self):
        """启动时汇总现存覆盖备份贴进日志区（薄壳，实现在 LogPanel._show_backup_notice）。"""
        self.log_panel._show_backup_notice()


