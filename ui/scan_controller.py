"""目录树扫描调度（TreeScanWorker 的起停 / 代际作废 / 批次灌树）。

行为冻结自 ui/main_window.py App 的 refresh / _abort_active_scan /
_drop_retired_scans / _on_items_ready / _on_scan_finished / _on_scan_error /
_collect_folder_items 方法族。红线原样保留：_retired_scans 退役保活 +
finished 摘除 + _scan_gen 代际作废模式——QThread 引用链不能断
（断则 0xc0000005 无痕闪退）。

与 App 的交互收窄为：
- 构造注入的同名回调（预警/记忆/默认勾选/引导页，实例属性直挂，函数体逐字搬）
- attach 移交的控件引用（entry_path/progress/root_warning_label/tree/
  status_label/log，_build_ui 创建后一次性 attach，refresh 只在此后被调用）
- folder_items 与 App 共享同一列表对象（App 从不重新赋值，只 clear/append）
树节点操作（_partial_refresh 等）留在 App——那是 UI。
"""

import os

from PyQt6.QtWidgets import QMessageBox
from PyQt6.QtCore import Qt

from core.guards import _is_protected_system_dir
from ui.workers import TreeScanWorker


class ScanController:
    """目录树扫描的拥有者：扫描线程、退役保活列表、代际号、勾选快照。"""

    def __init__(self, parent, folder_items, show_root_warning,
                 show_system_warning, save_parent_dir, update_tree_hint,
                 apply_default_checks, set_parent_dir):
        self._parent = parent  # QMessageBox 父窗口
        self.folder_items = folder_items  # 与 App 共享同一列表对象（仅文件夹 item）
        self._show_root_warning = show_root_warning
        self._show_system_warning = show_system_warning
        self._save_parent_dir = save_parent_dir
        self._update_tree_hint = update_tree_hint
        self.apply_default_checks = apply_default_checks
        self._set_parent_dir = set_parent_dir  # App.parent_dir 写入（App 侧普通属性）
        self.scan_worker = None  # 目录树扫描后台线程
        # 被 refresh() 中途 abort 但仍在运行的旧扫描线程保活列表：
        # 若只把 scan_worker 指向新线程，旧线程引用计数归零会被 sip 立即销毁
        # 运行中的 C++ QThread → 访问违规闪退；必须保活到其 finished 再释放。
        self._retired_scans = []
        self._scan_gen = 0       # 扫描代际号：refresh 每次 +1，旧线程信号按此丢弃
        self._full_snapshot = None
        self._previous_unchecked = []
        self._pending_summary = None
        # attach 前防御：refresh 等只在 _build_ui 完成后才会被调用
        self.entry_path = None
        self.progress = None
        self.root_warning_label = None
        self.tree = None
        self.status_label = None
        self.log = None

    def attach(self, entry_path, progress, root_warning_label, tree,
               status_label, log):
        """_build_ui 控件创建后移交引用（UI 不动，只搬家逻辑）。"""
        self.entry_path = entry_path
        self.progress = progress
        self.root_warning_label = root_warning_label
        self.tree = tree
        self.status_label = status_label
        self.log = log

    def _drop_retired_scans(self):
        """已跑完的退役扫描线程从保活列表移除（finished 信号触发，可能多个同时到）。"""
        self._retired_scans = [w for w in self._retired_scans if w.isRunning()]

    def _abort_active_scan(self):
        """中止在途扫描线程并递增代际号（refresh 换扫描 / 无效路径 / 清空目录共用）。
        gen 递增让旧线程已发出的滞留批次在接收槽被丢弃，不再把刚清空的树重新
        填回来；旧线程保活到真正结束（丢引用会被 sip 立即销毁运行中的
        C++ QThread → 访问违规闪退）。"""
        old_scan = getattr(self, "scan_worker", None)
        if old_scan is not None and old_scan.isRunning():
            old_scan.abort()
            self._retired_scans.append(old_scan)
            old_scan.finished.connect(self._drop_retired_scans)
        self._scan_gen += 1

    def refresh(self, preserve_full=False, snapshot=None):
        """扫描并填充目录树（异步、不卡 UI）。
        耗时扫描在 TreeScanWorker 子线程完成；主线程只负责『攒一批 → 一次性灌入』。
        preserve_full=True（撤回场景用）：扫描后按勾选快照原样恢复。
        snapshot：散开开始前拍下的勾选状态（App 按批次保存）。缺省时退回现拍
        当前树状态——仅供右键批次/跨会话撤回兜底，本地散开必须传 snapshot。"""
        base = self.entry_path.text().strip()
        # 刷新即回到初始空态进度条（同确认框点「否」的复位写法）：
        # 上次任务的 100%/忙碌动画不应残留到新一次扫描
        self.progress.setMaximum(100)
        self.progress.setValue(0)
        self.progress.setFormat("")
        self.root_warning_label.setVisible(False)  # 先清掉上次的红色根目录预警
        if not base or not os.path.isdir(base):
            # 在途扫描的滞留批次会把刚清空的树重新填回来，先中止并作废其代际
            self._abort_active_scan()
            self.tree.clear()
            self.folder_items.clear()
            self._update_tree_hint()  # 树已清空 → 显示空状态引导页
            return
        # abspath 含 normpath：手输相对路径（按进程 cwd 解析）统一转正为绝对路径，
        # 否则撤回记录无盘符，撤回时会被 _is_undo_path_allowed 当可疑记录跳过
        base = os.path.abspath(base)
        # 磁盘根目录保护：刷新的第一步就拦截，避免扫描整个磁盘导致卡顿
        _, rest = os.path.splitdrive(base)
        if rest in ("", "\\", "/"):
            # 与无效路径分支同款：先杀在途扫描并清树——否则旧目录的内容继续
            # 灌进树，与路径框显示的根目录对不上（F20）
            self._abort_active_scan()
            self.tree.clear()
            self.folder_items.clear()
            self._update_tree_hint()
            QMessageBox.warning(
                self._parent, "禁止操作",
                f"不允许直接对磁盘根目录（{base}）执行刷新或散开操作，请选择具体文件夹。")
            self._show_root_warning()
            return

        # 系统关键目录保护：刷新/加载前拦截
        if _is_protected_system_dir(base):
            # 同上方根目录分支：先杀在途扫描并清树（F20）
            self._abort_active_scan()
            self.tree.clear()
            self.folder_items.clear()
            self._update_tree_hint()
            QMessageBox.warning(
                self._parent, "禁止操作",
                f"不允许对系统关键目录（{base}）及其子目录执行刷新或散开操作。\n"
                "这会把系统/程序文件移出原位置，可能导致 Windows 崩溃或无法启动。\n"
                "请选择你自己的数据文件夹（如桌面、文档、下载、图片）。")
            self._show_system_warning()
            return
        # 换目录检测（须在 _set_parent_dir 之前读旧值）：只有「同一目录」的刷新
        # 才保留勾选记忆；换目录/进入子目录时，旧目录的未勾选记录对新树没有意义。
        # parent_dir 住在 App 上（本控制器只拿到 setter），必须从 _parent 读——
        # 读自身会永远得到空串，_same_base 恒 False，同目录刷新也会丢勾选记忆
        _old_base = os.path.normcase(os.path.normpath(
            getattr(self._parent, "parent_dir", "") or ""))
        _same_base = bool(_old_base) and _old_base == os.path.normcase(os.path.normpath(base))
        self._set_parent_dir(base)
        self._save_parent_dir()  # 父目录变更 → 记忆设置

        # 若上一次扫描还在跑：不再 quit+wait 硬等（quit 对无事件循环的线程无效，
        # wait 会让巨型目录卡死 UI）。打中止标记后起新线程即可；
        # 旧线程的信号带旧代际号，会被接收槽丢弃。
        self._abort_active_scan()

        # 完整保真快照（撤回时 preserve_full=True 用）：优先用散开开始前拍下的
        # 快照。撤回时现拍会失真——此刻树已被散开 + 增量刷新改过，拍到的是
        # 「散开后」的勾选（用户实测：散开前全选，撤回后只剩零星几个保持勾选）。
        self._full_snapshot = None
        if preserve_full:
            if snapshot:
                self._full_snapshot = dict(snapshot)
            else:
                self._full_snapshot = {}
                for _it in getattr(self, "folder_items", []):
                    _d = _it.data(0, Qt.ItemDataRole.UserRole)
                    if _d and _d.get("is_dir"):
                        self._full_snapshot[os.path.normpath(_d["path"])] = (
                            _it.checkState(0) == Qt.CheckState.Checked)

        # 记录当前未勾选的文件夹路径，扫描完成后恢复，
        # 避免刷新（或拖入新路径）时 apply_default_checks 把用户手动取消的勾选强制覆盖成全选
        # ——仅限同一目录刷新；换目录时置空，新目录按默认勾选（顶层勾、深层不勾）
        self._previous_unchecked = []
        if _same_base:
            for _it in getattr(self, "folder_items", []):
                if _it.checkState(0) == Qt.CheckState.Unchecked:
                    _d = _it.data(0, Qt.ItemDataRole.UserRole)
                    if _d and _d.get("is_dir"):
                        self._previous_unchecked.append(os.path.normpath(_d["path"]))

        self.tree.clear()
        self.folder_items.clear()

        # === 提速核心：先关掉界面刷新，等整批数据灌完后一次性重绘 ===
        self.tree.setUpdatesEnabled(False)
        self.status_label.setText("正在扫描目录…")

        self.scan_worker = TreeScanWorker(base, self._scan_gen)
        self.scan_worker.items_ready.connect(self._on_items_ready)
        self.scan_worker.scan_finished.connect(self._on_scan_finished)
        self.scan_worker.scan_error.connect(self._on_scan_error)
        self.scan_worker.start()

    def _on_items_ready(self, batch, gen):
        """主线程收到一批顶级节点：保持更新禁用，批量添加（不逐条重绘）。
        代际不符（旧线程在 abort 前已发出的滞留信号）直接丢弃，防污染新树。"""
        if gen != self._scan_gen:
            return
        self.tree.setUpdatesEnabled(False)
        self.tree.addTopLevelItems(batch)
        self._update_tree_hint()  # 首批节点到达即从引导页切回目录树

    def _on_scan_finished(self, gen):
        """全部批次加完：恢复刷新 → 一次性重绘 → 收集文件夹 item → 应用勾选。"""
        if gen != self._scan_gen:
            return
        self.tree.setUpdatesEnabled(True)
        self._collect_folder_items()
        snap = getattr(self, "_full_snapshot", None)
        if snap:
            # 完整保真还原（撤回场景）：已存在的文件夹按快照恢复勾选状态；
            # 新出现的（被还原出来的）散开的源文件夹默认勾选（与散开前一致），不触发“全选”。
            for _it in self.folder_items:
                _d = _it.data(0, Qt.ItemDataRole.UserRole)
                if _d and _d.get("is_dir"):
                    _p = os.path.normpath(_d["path"])
                    if _p in snap:
                        _it.setCheckState(
                            0, Qt.CheckState.Checked if snap[_p] else Qt.CheckState.Unchecked)
                    else:
                        # 新出现的（被还原出来的）文件夹：只勾顶层源文件夹（与散开前
                        # 默认一致）；深层子文件夹散开前本就未勾选，若一律恢复成勾选，
                        # 撤回后随手再散一次就把所有层级都卷进去了
                        _base = getattr(self, "parent_dir", "") or ""
                        _depth = _d["path"].count(os.sep) - (
                            _base.count(os.sep) if _base else 0)
                        _it.setCheckState(
                            0, Qt.CheckState.Checked if _depth == 1 else Qt.CheckState.Unchecked)
            self._full_snapshot = None
        else:
            self.apply_default_checks()
            # 恢复用户之前手动取消勾选的文件夹，避免被默认勾选覆盖
            _prev = getattr(self, "_previous_unchecked", [])
            if _prev:
                _prev_set = set(_prev)
                for _it in self.folder_items:
                    _d = _it.data(0, Qt.ItemDataRole.UserRole)
                    if _d and _d.get("is_dir") and os.path.normpath(_d["path"]) in _prev_set:
                        _it.setCheckState(0, Qt.CheckState.Unchecked)
                self._previous_unchecked = []
        self._update_tree_hint()  # 扫描结果为空（如无子文件夹）→ 显示引导页
        # 散开完成时挂起的结果摘要优先于「目录加载完成」显示（完成闭环）
        if getattr(self, "_pending_summary", None):
            self.status_label.setText(self._pending_summary)
            self._pending_summary = None
        else:
            self.status_label.setText("目录加载完成")

    def _on_scan_error(self, msg, gen):
        if gen != self._scan_gen:
            return
        self.tree.setUpdatesEnabled(True)
        self._update_tree_hint()
        self._pending_summary = None
        self.status_label.setText("目录加载失败")
        self.log.append(f"[错误] 扫描目录异常：{msg}")

    def _collect_folder_items(self):
        """遍历已填充的目录树，收集所有文件夹 item（folder_items）。
        这一步在『全部加完、恢复刷新』之后于主线程执行，保证只访问 GUI 对象。"""
        self.folder_items.clear()

        def walk(item):
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if data and data.get("is_dir"):
                self.folder_items.append(item)
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))
