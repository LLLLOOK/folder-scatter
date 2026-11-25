"""App 拆分子模块：散开执行（开始/模拟/停止）与撤回（自 ui/main_window.py 逐字搬移，行为冻结）。

RunMixin 无任何 __init__，全部方法仍写 self（经 App 多继承解析），调用面零改动。
"""

import os

from PyQt6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QMessageBox,
)

from PyQt6.QtCore import (
    Qt,
)

from win32.taskbar import TaskbarProgress
from core.guards import _is_protected_system_dir
from undo.restore import UndoWorker
from undo.store import _clear_inflight
from ui.workers import Worker


class RunMixin:
    # ---------- 执行入口（开始 / 模拟） ----------
    def start(self):
        self._begin_run(simulate=False)

    def simulate(self):
        self._begin_run(simulate=True)

    def _begin_run(self, simulate):
        if self.worker is not None and self.worker.isRunning():
            return
        raw = self.entry_path.text().strip()
        # 空输入直接判无效：normpath("")=="." 会误成进程当前目录，既弹怪弹窗
        # 又可能把散开指向错误位置
        if not raw or not os.path.isdir(raw):
            QMessageBox.critical(self, "错误", "请先选择或输入父目录。")
            return
        base = os.path.abspath(raw)  # 含 normpath；相对路径转正，与 refresh 保持一致

        # 输入框路径与目录树不一致（改了路径没按回车）：勾选项来自旧目录树，
        # 继续会把旧目录的文件夹散到新路径——拦截并要求先刷新对齐
        if os.path.normcase(base) != os.path.normcase(os.path.normpath(self.parent_dir or "")):
            QMessageBox.warning(
                self, "路径未刷新",
                "输入框路径与目录树不一致，本次操作已取消。\n"
                "请先按回车或点「刷新」。")
            return

        # 磁盘根目录保护
        drive, rest = os.path.splitdrive(base)
        if drive and rest in ("", "\\", "/"):
            QMessageBox.critical(
                self, "禁止操作",
                f"不允许直接对磁盘根目录（{base}）执行散开操作，请选择具体文件夹。")
            return

        # 系统关键目录保护（防误把 Windows/Program Files 等散开导致系统崩溃）
        if _is_protected_system_dir(base):
            QMessageBox.critical(
                self, "禁止操作",
                f"不允许对系统关键目录（{base}）及其子目录执行散开操作。\n"
                "这会把系统/程序文件移出原位置，可能导致 Windows 崩溃或无法启动。\n"
                "请选择你自己的数据文件夹（如桌面、文档、下载、图片）。")
            return

        selected = [it.data(0, Qt.ItemDataRole.UserRole)["path"]
                    for it in self.folder_items
                    if it.checkState(0) == Qt.CheckState.Checked]
        if not selected:
            QMessageBox.information(self, "提示", "没有勾选任何文件夹")
            return

        # 非法后缀拦截：输入框红框状态下不允许开始（含模拟），否则「jpg!!」这类
        # 输入会静默变成永不匹配的过滤器（只散开 0 命中、只排除全放行）
        if not getattr(self, "_custom_suffix_valid", True):
            QMessageBox.warning(
                self, "无法开始",
                "自定义后缀里有无法识别的内容（只允许字母和数字，多个后缀用空格或逗号分隔）。\n"
                "请改正后缀输入框，或把它清空。")
            return

        suffixes = self._parse_suffixes()
        max_size = self.spin_max.value() if self.chk_ignore.isChecked() else 0

        mode_text = {'move': '移动', 'copy': '复制'}[self.mode]
        conflict_text = {'overwrite': '覆盖', 'skip': '跳过',
                         'rename': '自动重命名'}[self.conflict]
        level_text = {'one': '仅当前层', 'two': '包含子文件夹',
                      'all': '全部层级'}[self.level]

        # 先进入忙碌态：用户点「开始散开」后立即看到进度条动画。
        # 真实上限由子线程扫描完目录后通过 maxsig 再设置，避免深层目录卡 UI。
        self.progress.setMaximum(0)   # 0=不确定进度
        self.progress.setValue(0)
        self.progress.setFormat("")   # 忙碌/确认期间不显示百分比
        self.skip_label.setText("")
        self.skip_label.setVisible(False)

        if not simulate and bool(self.settings.value(
                "confirm_scatter", True, type=bool)):
            mb = QMessageBox(self)
            mb.setIcon(QMessageBox.Icon.Question)
            mb.setWindowTitle("确认")
            # 「深度」对应散开深度分组（原误标为"模式"）；勾选了覆盖备份时
            # 补一句安抚文案，没勾则只提示"冲突时覆盖"
            backup_line = (
                "覆盖前旧文件会先备份，撤回可还原。\n"
                if self.conflict == "overwrite" and self.chk_trash.isChecked()
                else "")
            mb.setText(
                f"深度：{level_text}\n"
                f"目标父目录：{base}\n\n"
                f"将对 {len(selected)} 个勾选的文件夹执行"
                f"{mode_text}（冲突时{conflict_text}）。\n"
                f"{backup_line}"
                "提示：不确定影响范围？可先点「模拟运行」预览清单，"
                "不会产生任何改动。\n\n确定继续？")
            # 「不再提示」：勾选并点「是」才持久化关闭确认（点「否」不改，防误关）
            chk_no_ask = QCheckBox("不再提示（以后直接开始）")
            mb.setCheckBox(chk_no_ask)
            yes_btn = mb.addButton("是", QMessageBox.ButtonRole.YesRole)
            mb.addButton("否", QMessageBox.ButtonRole.NoRole)
            mb.setDefaultButton(yes_btn)
            # QMessageBox 在文字与按钮之间留了空占位行，双份行距使底部明显比
            # 上边距松：把行距压到约一半，「确定继续？」到按钮的间距≈上边距
            _lay = mb.layout()
            if isinstance(_lay, QGridLayout):
                _lay.setVerticalSpacing(
                    max(1, _lay.contentsMargins().top() // 2))
            mb.exec()
            if mb.clickedButton() is not yes_btn:
                # 点「否」：把进度条重置回默认静止空态（不显示 0%）
                self.progress.setMaximum(100)
                self.progress.setValue(0)
                self.progress.setFormat("")
                return
            if chk_no_ask.isChecked():
                self.settings.setValue("confirm_scatter", False)

        # 真正开始执行：恢复百分比显示
        self.progress.setFormat("%p%")
        # 任务栏同步：本地散开同样显示进度（先忙碌态，扫描出总数后转绿色百分比）
        if not simulate and self._taskbar_supported:
            self._tb_local_total = 0
            try:
                TaskbarProgress.set_indeterminate(int(self.winId()))
            except Exception:
                pass

        self.log_msg(f"{'[预览] ' if simulate else ''}开始处理：{level_text}，"
                     f"{len(selected)} 个勾选文件夹，模式={mode_text}，冲突={conflict_text}"
                     + (f"，后缀过滤={','.join(sorted(suffixes))}" if suffixes else "，后缀=全部")
                     + (f"，忽略>{max_size}MB" if max_size > 0 else ""))

        # 散开前拍下目录树勾选状态：撤回完成后按它恢复。撤回时现拍会拍到
        # 「散开后」的状态——散开前全选，撤回后却只剩零星几个保持勾选（用户实测）
        if not simulate:
            _snap = {}
            for _it in self.folder_items:
                _d = _it.data(0, Qt.ItemDataRole.UserRole)
                if _d and _d.get("is_dir"):
                    _snap[os.path.normpath(_d["path"])] = (
                        _it.checkState(0) == Qt.CheckState.Checked)
            self._pre_scatter_checks = _snap

        # 锁定按钮，启动子线程
        self.btn_start.setEnabled(False)
        self.btn_simulate.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_undo.setEnabled(False)

        self.worker = Worker(selected, base, self.mode, self.conflict,
                             suffixes, max_size, simulate,
                             skip_hidden=bool(self.settings.value(
                                 "ignore_hidden", False, type=bool)),
                             to_trash=self.chk_trash.isChecked(),
                             level=self.level,
                             suffix_exclude=self.filter_exclude)
        self.worker.logmsg.connect(self.log_msg)
        self.worker.progresssig.connect(self.progress.setValue)
        self.worker.maxsig.connect(self.progress.setMaximum)
        if not simulate:
            self.worker.progresssig.connect(self._taskbar_local_prog)
            self.worker.maxsig.connect(self._taskbar_local_max)
        self.worker.skipsig.connect(self._on_skip)
        self.worker.finishedsig.connect(self._on_worker_finished)
        self.worker.start()

    def _on_skip(self, skipped, occupied, permission):
        self._skip_counts = (skipped, occupied, permission)
        self._refresh_skip_label()

    def _refresh_skip_label(self):
        """用当前主题颜色重建警示条 HTML（主题切换时调用）"""
        skipped, occupied, permission = self._skip_counts
        if skipped == 0 and occupied == 0 and permission == 0:
            return
        parts = ["⚠️ 本次"]
        first = True

        def add(text, color):
            nonlocal first
            if not first:
                parts.append("、")
            parts.append(f"<font color='{color}'>{text}</font>")
            first = False

        if skipped > 0:
            add(f"跳过 {skipped} 个大文件", self._log_warn)
        if occupied > 0:
            add(f"遇到 {occupied} 个被占用文件", self._log_error)
        if permission > 0:
            add(f"{permission} 个权限不足", self._log_error)

        self.skip_label.setText("".join(parts))
        self.skip_label.setVisible(True)

    def _on_worker_finished(self):
        self.btn_start.setEnabled(True)
        self.btn_simulate.setEnabled(True)
        self.btn_stop.setEnabled(False)
        w = self.worker
        if w is not None and not w.simulate:
            if w.undo_records:
                _batch = list(w.undo_records)
                self.undo_stack.append(_batch)
                # 勾选快照跟着批次走：撤回这一批时恢复散开前的勾选布局
                if getattr(self, "_pre_scatter_checks", None):
                    self._check_snapshots[id(_batch)] = self._pre_scatter_checks
                    self._pre_scatter_checks = None
                self._last_undo_group = None  # 本地散开批次无分组，阻断后续右键批次误并
                if self._save_undo_history():  # 持久化本次散开的撤回批次
                    _clear_inflight()  # B1：批次已安全落盘，清增量账本
            self.progress.setValue(self.progress.maximum())
            self._taskbar_done()  # 任务栏置满，1.5s 后自动清除
        else:
            # 模拟运行结束：进度条回空态（不留半途的百分比值）
            self.progress.setMaximum(100)
            self.progress.setValue(0)
            self.progress.setFormat("")
        # 完成摘要：状态栏一行总览（详细 [总结] 已在日志里）。
        # 若下面触发了全量 refresh，扫描完成信号会覆盖状态栏
        # → 摘要挂起到 _pending_summary，由 _on_scan_finished 消费
        self._pending_summary = None
        if w is not None:
            if w.simulate:
                self._pending_summary = (
                    f"模拟完成：预计散开 {w.moved_count} 个文件（未做任何实际改动）")
            else:
                _verb = "移动" if w.mode == "move" else "复制"
                _parts = [f"完成：{_verb} {w.moved_count} 个文件"]
                if w.last_skip_stats[0]:
                    _parts.append(f"跳过 {w.last_skip_stats[0]} 个大文件")
                if w.err_count:
                    _parts.append(f"错误 {w.err_count} 个（详见日志）")
                self._pending_summary = "，".join(_parts)
        # 散开后局部更新树（增量），不再全树重扫。
        # 例外：move 模式下有源文件夹残留（被过滤/占用未搬完、merge 进已存在目录），
        # 残留节点的子项已变化而增量刷新不会重扫它们 → 回退全量 refresh 保证树准确
        _selected = getattr(w, "selected", None)
        # 兜底：树更新失败只警告，绝不让槽函数异常导致闪退
        try:
            if (w is not None and not w.simulate
                    and w.mode == "move"
                    and any(os.path.isdir(p) for p in (_selected or []))):
                self.refresh()
            else:
                self._partial_refresh(_selected)
                # _partial_refresh 内部新增项 >300 时可能回退全量 refresh；
                # 扫描线程在跑则摘要留给 _on_scan_finished，否则立即显示
                if (self._pending_summary
                        and not (self.scan_worker is not None and self.scan_worker.isRunning())):
                    self.status_label.setText(self._pending_summary)
                    self._pending_summary = None
        except Exception as e:
            self.log_msg(f"[警告] 散开后更新目录树失败：{e}")
        # 完成后自动清空日志
        if bool(self.settings.value("auto_clear_log", False, type=bool)):
            self.log.clear()
            self._log_overflow_notified = False  # 清空后再次触顶会重新提示
        # 释放 worker 引用，避免 QThread 对象在任务结束后仍驻留内存（反复散开会缓慢泄漏）。
        # scan_worker 仅在未运行时才置 None——散开期间用户可能又触发了刷新，
        # 不能把正在跑的扫描线程引用丢掉（否则退出时无法 abort 它）
        self.worker = None
        # 本地散开期间延后（deferred）的右键撤回批次，此刻统一压栈（同组续批合并）。
        # 必须在 self.worker=None 之后并走 _push_undo_batch：撤回仍在进行时直接
        # 并栈会 extend 正被撤回的栈顶批次，_on_undo_finished 用 remaining 替换
        # 栈顶时这批记录会被弄丢；忙碌时 _push_undo_batch 会自动再延后
        if self._deferred_undo:
            _pend = self._deferred_undo
            self._deferred_undo = []
            for b, g, bids in _pend:
                self._push_undo_batch(b, g, bids)
        if self.scan_worker is not None and not self.scan_worker.isRunning():
            self.scan_worker = None
        # 撤回按钮按栈实况统一恢复：_begin_run 无条件置灰（防边散开边撤回），
        # 模拟运行/零记录散开（后缀全过滤、锁冲突等）不产生新批次，若只在
        # 「有新记录」时点亮，栈里明明有旧批次按钮却永久卡灰（H1）
        self.btn_undo.setEnabled(bool(self.undo_stack))

    def stop_run(self):
        if self.worker is not None:
            self.worker.request_stop()
            self.log_msg("[停止] 正在中止...")

    # ---------- 撤回 ----------
    def undo(self):
        if not self.undo_stack:
            return
        if getattr(self, "undo_worker", None) is not None and self.undo_worker.isRunning():
            return  # 上一次撤回仍在进行（按钮已禁用，此处仅兜底）
        # 撤回与散开共用同一把命名互斥体（所有权属于线程），由 UndoWorker 在
        # 自己线程内获取/释放；此处先看不取——锁冲突时 lockfailed 保持栈原样。
        batch = self.undo_stack[-1]
        self.log_msg("[撤回] 开始还原上一次操作...")
        # 撤回在子线程执行，锁定期按钮；期间点开始散开会被同一把锁拒绝并打红日志
        self.btn_undo.setEnabled(False)
        self.btn_start.setEnabled(False)
        self.btn_simulate.setEnabled(False)
        self.undo_worker = UndoWorker(batch)
        self.undo_worker.logmsg.connect(self.log_msg)
        self.undo_worker.lockfailed.connect(self._on_undo_lock_failed)
        self.undo_worker.finishedsig.connect(self._on_undo_finished)
        self.undo_worker.start()

    def _on_undo_lock_failed(self, reason):
        """锁被他人（本地散开/右键进程）持有：撤回未执行，栈原样保留，可稍后再试。"""
        self.log_msg(f"[错误] {reason}")
        self._last_undo_group = None  # 栈顶可能将被移除，阻断后续右键批次误并
        self.btn_start.setEnabled(True)
        self.btn_simulate.setEnabled(True)
        self.btn_undo.setEnabled(True)
        self.undo_worker = None

    def _on_undo_finished(self):
        # 撤回结束后处理本批次（按对象身份定位：期间可能有 deferred 右键批次压栈，
        # 且不同批次内容可能恰好相等，不能用 == 比较）
        uw = self.undo_worker
        if uw is not None:
            rem = getattr(uw, "remaining", None) or []
            for i in range(len(self.undo_stack) - 1, -1, -1):
                if self.undo_stack[i] is uw.batch:
                    if rem:
                        self.undo_stack[i] = rem  # 未完成/失败项留栈可重试
                    else:
                        del self.undo_stack[i]
                    break
            if rem:
                self.log_msg(f"[警告] {len(rem)} 项未还原（失败或被中止），"
                             f"已保留在撤回栈，可再次点撤回重试。")
            else:
                # 整批已撤回：该批的勾选快照消费完毕，删掉防忘
                getattr(self, "_check_snapshots", {}).pop(id(uw.batch), None)
            self._last_undo_group = None  # 栈结构已变，阻断后续右键批次误并
            self._save_undo_history()  # 栈变化后持久化
        self.btn_start.setEnabled(True)
        self.btn_simulate.setEnabled(True)
        self.undo_worker = None
        # 撤回进行中被延后的右键批次现在才压栈（避免 extend 正在被迭代的批次）
        if self._deferred_undo and self.worker is None:
            pend = self._deferred_undo
            self._deferred_undo = []
            for recs, group, bids in pend:
                self._push_undo_batch(recs, group, bids)
        self.btn_undo.setEnabled(bool(self.undo_stack))
        # 完整保真重新扫描目录树：被还原的文件夹自动回到目录树，
        # 其余文件夹按【散开前】的勾选快照原样恢复（不触发“全选”）。
        # 快照按批次保存；右键批次没有快照 → None → 扫描器退回现拍兜底。
        _snap = None
        if uw is not None:
            _snap = getattr(self, "_check_snapshots", {}).get(id(uw.batch))
        # 兜底：刷新失败只警告，绝不让槽函数异常导致闪退
        try:
            self.refresh(preserve_full=True, snapshot=_snap)
        except Exception as e:
            self.log_msg(f"[警告] 撤回后刷新目录树失败：{e}")
