# ui/log_panel.py —— 日志区（LogPanel）
#
# 从 ui/main_window.py App 上帝类抽出的日志逻辑整体收口：
#   · 彩色日志渲染 log_msg（[总结]/[错误]/[警告]/[撤回] 等着色规则）
#   · 清空/复制错误/导出（clear_log/_collect_errors/copy_errors/export_log）
#   · 启动期日志（_load_history_log 右键失败摘要 / _show_crash_log 闪退原因）
#   · [备份] 提示联动（_sync_backup_notice_log 原地重写 / _show_backup_notice 启动汇总）
#   · 主题重灌 apply_theme（_log_* 颜色族 + 已有日志按新主题重新着色）
#
# 红线（逐字保留，一个字符未改）：
#   · 贴底跟随三处同策略：log_msg / apply_theme 重灌 / App.resizeEvent，
#     容差 maximum - value <= 4（resizeEvent 是窗口事件，本体留 App，策略逐字一致）
#   · 触顶警告 _log_overflow_notified 语义：首次触顶打一条 [警告]；
#     手动/自动清空日志重置，主题切换重灌不重置
#
# 与 App 的交互收窄为构造注入的回调：status（状态栏提示）、
# collect_backup_dirs（备份目录汇总，薄壳在 App）。日志控件本体（QTextEdit）
# 仍在 App._build_ui 创建（UI 不动），创建后 attach 给本类持有；
# App.log_msg 保留薄壳转发（worker/undo_worker 信号已 connect 它，不动）。

import os
import re
import time

from PyQt6.QtWidgets import QApplication, QFileDialog
from PyQt6.QtGui import QColor, QTextCharFormat, QTextCursor

from infra.logutil import LOG_MAX_BLOCKS, _trim_log_if_big
from infra.paths import CRASH_LOG, _undo_base
from ui.style import DARK_BG, DARK_FG, LIGHT_BG, LIGHT_FG, scrollbar_qss


class LogPanel:
    """日志区逻辑持有者（非控件）：QTextEdit 由 App._build_ui 创建后 attach。"""

    def __init__(self, status, collect_backup_dirs, parent=None):
        self.log = None  # QTextEdit，App._build_ui 创建后 attach
        self._status = status  # 状态栏提示回调（status_label.setText）
        self._collect_backup_dirs_cb = collect_backup_dirs
        self._parent = parent  # 文件对话框父窗口
        self._log_overflow_notified = False
        self._log_fg = LIGHT_FG
        # 初始默认与浅色主题一致（启动后 apply_theme 会按实际主题重设）
        self._log_success = "#1B5E20"
        self._log_summary = "#2E7D32"
        self._log_error = "#C62828"
        self._log_warn = "#8A5A00"
        self._log_warn_orange = "#BF360C"
        self._log_undo = "#006064"

    def attach(self, log):
        """接管 App._build_ui 创建的日志控件（UI 不动，仅移交持有）。"""
        self.log = log

    # ---------- 主题 ----------
    def apply_theme(self, is_dark):
        """按主题重设 _log_* 颜色族，并按新主题重新着色已有日志文本。"""
        self._log_fg = DARK_FG if is_dark else LIGHT_FG
        self._log_success = "#A5D6A7" if is_dark else "#1B5E20"
        self._log_summary = "#81C784" if is_dark else "#2E7D32"
        self._log_error = "#EF5350" if is_dark else "#C62828"
        self._log_warn = "#FFCA28" if is_dark else "#8A5A00"
        self._log_warn_orange = "#FF9800" if is_dark else "#BF360C"
        self._log_undo = "#26C6DA" if is_dark else "#006064"
        if self.log is not None:
            _log_bg = DARK_BG if is_dark else LIGHT_BG
            _log_border = "#404040" if is_dark else "#cccccc"
            # 局部样式表必须带上滚动条规则：日志控件有自己的 QSS 时，滚动条
            # 也在这张表里取样式，漏写会退回系统默认的「带框凹槽」
            self.log.setStyleSheet(
                f"background-color: {_log_bg}; border: 1px solid {_log_border}; "
                "border-radius: 4px; font-family: Consolas, 'Microsoft YaHei', sans-serif;"
                + scrollbar_qss(is_dark))
            # 主题切换后重新着色已有日志文本（QTextCharFormat 颜色在插入时固化，不会自动跟随主题）
            _log_text = self.log.toPlainText()
            if _log_text.strip():
                # 重灌前记下滚动位置：clear 后「处处皆底部」，log_msg 的贴底
                # 跟随判定会失真——按切换前的位置恢复，修复「切主题日志自动
                # 跳到最下」（BUG.txt）
                _sb = self.log.verticalScrollBar()
                _at_bottom = _sb.maximum() - _sb.value() <= 4
                _old_pos = _sb.value()
                self.log.clear()
                self.log_msg(_log_text)
                if _at_bottom:
                    self.log.moveCursor(QTextCursor.MoveOperation.End)
                    self.log.ensureCursorVisible()
                else:
                    _sb.setValue(min(_old_pos, _sb.maximum()))

    # ---------- 日志（彩色） ----------
    def log_msg(self, msg):
        # 防御：日志控件尚未创建时直接跳过，避免启动期崩溃
        if not hasattr(self, "log") or self.log is None:
            return
        # 只在原本就贴着底部时才跟随滚动：万级日志刷屏时用户正往上翻看错误，
        # 无条件拽底会让上翻永远失效；滚回底部后恢复跟随
        _sb = self.log.verticalScrollBar()
        _was_bottom = _sb.maximum() - _sb.value() <= 4
        cur = self.log.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        for line in msg.splitlines():
            if line.startswith("[总结]"):
                # 看情况：有跳过/被占用/权限不足 → 警示黄，否则亮绿（阶段结束）
                if "跳过" in line or "被占用" in line or "权限不足" in line:
                    color = self._log_warn
                else:
                    color = self._log_summary
                # 先转义用户可控文本（路径可能含 & < >），再注入自己的颜色标签，
                # 防 HTML 渲染错乱 / 富文本加载外部资源
                colored = (line.replace("&", "&amp;").replace("<", "&lt;")
                           .replace(">", "&gt;"))
                # 让「跳过 X 个大文件」显黄色，「遇到 X 个被占用文件」显错误同色红
                colored = re.sub(r"跳过 (\d+) 个大文件",
                    rf"<font color='{self._log_warn}'>跳过 \1 个大文件</font>", colored)
                colored = re.sub(r"遇到 (\d+) 个被占用文件",
                    rf"<font color='{self._log_error}'>遇到 \1 个被占用文件</font>", colored)
                # 提示语里的"红色/黄色"也按对应日志色着色
                colored = colored.replace("红色", f"<font color='{self._log_error}'>红色</font>")
                colored = colored.replace("黄色", f"<font color='{self._log_warn}'>黄色</font>")
                html = f"<span style='color: {color}; font-weight: 500;'>{colored}</span>"
                cur.insertHtml(html + "<br>")
                continue
            color = self._log_fg
            _bold = False
            if line.startswith("[错误]"):
                color = self._log_error
            elif line.startswith(("[跳过]", "[建议]")):
                color = self._log_warn
            elif line.startswith("[警告]"):
                color = self._log_warn_orange  # 警告用橙色，比跳过更醒目
            elif line.startswith("[完成]"):
                color = self._log_summary  # 阶段结束：亮绿（深色）/深绿+粗体（浅色）
                _bold = True
            elif line.startswith("全部完成"):
                color = self._log_summary
                _bold = True
            elif line.startswith("[撤回] 已将副本送入回收站"):
                color = self._log_warn_orange  # 副本进回收站：与[警告]同橙，醒目区分
            elif line.startswith("[撤回]"):
                color = self._log_undo  # 撤回：青绿色，在绿色日志中一眼区分
            elif line.startswith(("[move", "[copy", "[移动]", "[复制]", "[预览]", "开始处理")):
                color = self._log_success  # 执行动作：标准绿
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if _bold:
                fmt.setFontWeight(500)
            cur.insertText(line + "\n", fmt)
        # 首次触发行数上限：更早的日志已被自动清理，打一条提示（只此一次；
        # 手动/自动清空日志后重置标记，主题切换重灌不重置——防重复提示）
        if not getattr(self, "_log_overflow_notified", True) and \
                self.log.document().blockCount() >= LOG_MAX_BLOCKS:
            self._log_overflow_notified = True
            _fmt_notice = QTextCharFormat()
            _fmt_notice.setForeground(QColor(self._log_warn_orange))
            cur.insertText(
                f"[警告] 日志已达 {LOG_MAX_BLOCKS} 行上限，更早的日志已自动清理"
                "（复制错误/导出仅包含最近内容）。\n", _fmt_notice)
        self.log.setTextCursor(cur)
        if _was_bottom:
            self.log.ensureCursorVisible()

    def clear_log(self):
        self.log.clear()
        self._log_overflow_notified = False  # 清空后再次触顶会重新提示

    # ---------- 导出日志 / 复制错误 ----------
    def _collect_errors(self):
        """从日志纯文本中提取全部 [错误] 行及其紧随的 [建议] 行。"""
        if not hasattr(self, "log") or self.log is None:
            return []
        text = self.log.toPlainText()
        keep, prev_err = [], False
        for line in text.splitlines():
            if line.startswith("[错误]"):
                keep.append(line)
                prev_err = True
            elif prev_err and line.startswith("[建议]"):
                keep.append(line)
                prev_err = False
            else:
                prev_err = False
        return keep

    def copy_errors(self):
        """把日志中全部 [错误] 行（含其后 [建议]）复制到剪贴板，便于直接发给同事。"""
        keep = self._collect_errors()
        if not keep:
            self._status("当前日志中没有 [错误] 记录，无需复制。")
            return
        n_err = sum(1 for l in keep if l.startswith("[错误]"))
        QApplication.instance().clipboard().setText("\n".join(keep))
        self._status(f"已复制 {n_err} 条错误到剪贴板，可直接粘贴给同事/求助。")

    def export_log(self):
        """把当前整份日志另存为 UTF-8(BOM) 文本文件，记事本打开不乱码。"""
        if not hasattr(self, "log") or self.log is None:
            return
        text = self.log.toPlainText()
        if not text.strip():
            self._status("日志为空，无可导出内容。")
            return
        default_name = "散开日志_" + time.strftime("%Y%m%d_%H%M%S") + ".txt"
        path, _ = QFileDialog.getSaveFileName(
            self._parent, "导出日志", default_name, "文本文件 (*.txt);;所有文件 (*.*)")
        if not path:
            return
        try:
            with open(path, "wb") as f:
                f.write(b"\xef\xbb\xbf")  # UTF-8 BOM，避免记事本按 ANSI 解码乱码
                f.write(text.encode("utf-8"))
            self._status(f"日志已导出：{path}")
        except Exception as e:
            self._status(f"[错误] 导出日志失败：{e}")

    # ---------- 启动期日志（右键失败摘要 / 闪退原因） ----------
    def _load_history_log(self):
        """启动时仅当『上次右键散开失败』才把失败日志载入日志框；
        其余情况日志框保持空白（正常散开日志在散开时实时记录，撤回日志在点击撤回时记录）。
        显示一次后将该失败会话从 silent.log 清除，避免每次打开都重复刷屏。"""
        try:
            hist_log = os.path.join(_undo_base, "silent.log")
            _trim_log_if_big(hist_log)  # 超 3MB 自动裁剪到 1MB，防无限增长
            if not os.path.exists(hist_log):
                return
            # 只读尾部 1MB：silent.log 无限累积，全量读会让启动变慢/内存尖峰
            # （与 _append_utf8_bom 的 5MB 防 OOM 阈值同一思路）；
            # 一次右键会话通常几十行，尾部窗口足够覆盖最后一次会话
            with open(hist_log, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 1024 * 1024))
                if size > 1024 * 1024:
                    f.readline()  # 丢弃可能被截断的首行
                text = f.read().decode("utf-8-sig", errors="replace")
            lines = text.splitlines()
            if not lines:
                return
            # 按会话切分：每个右键会话以「[调试] 收到文件夹路径」起始
            last_start = 0
            for i, raw in enumerate(lines):
                if "[调试] 收到文件夹路径:" in raw:
                    last_start = i
            last_session = lines[last_start:]
            # 仅当最后一次会话以失败告终才显示
            failed = any("[错误] 右键散开失败" in ln for ln in last_session)
            if not failed:
                return
            # 紧凑摘要：只摘失败行（最多 3 条）+ 总数，不把整段会话刷进日志框
            ts = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")
            fails = []
            for raw in last_session:
                content = ts.sub("", raw, count=1).strip()
                if content.startswith("[错误] 右键散开失败"):
                    fails.append(content)
            self.log_msg(f"[历史] 上次右键散开有 {len(fails)} 个失败"
                         + (f"（展示前 3 条）：" if fails else "。"))
            for ln in fails[:3]:
                self.log_msg(ln)
            if len(fails) > 3:
                self.log_msg(f"[历史] …另有 {len(fails) - 3} 个，详见 silent.log。")
            # 显示一次后清除该失败会话，下次打开不再重复出现。
            # 注意：上面只读了尾部 1MB，重写前必须按完整内容重读来定位会话起点，
            # 否则会把窗口之前的日志一起丢掉；文件过大（>20MB）时放弃重写，
            # 宁可下次再显示一次，也不冒整文件入内存的风险
            try:
                if size <= 20 * 1024 * 1024:
                    with open(hist_log, "r", encoding="utf-8-sig") as f:
                        full_lines = f.read().splitlines()
                    full_start = 0
                    for i, raw in enumerate(full_lines):
                        if "[调试] 收到文件夹路径:" in raw:
                            full_start = i
                    kept = full_lines[:full_start]
                    # 原子重写：先写 .tmp 再 os.replace（同目录必同盘），避免 w 模式
                    # 打开即截断后写失败（磁盘满/杀软扫描）把 silent.log 清成 0 字节
                    tmp_log = hist_log + ".tmp"
                    with open(tmp_log, "w", encoding="utf-8-sig") as f:
                        if kept:
                            f.write("\n".join(kept) + "\n")
                        # kept 为空时文件为 0 字节（utf-8-sig 的 BOM 在首次 write
                        # 才懒写入）；下次 _append_utf8_bom 追加时会自动补回 BOM
                    os.replace(tmp_log, hist_log)
            except Exception:
                pass
        except Exception:
            pass

    def _show_crash_log(self):
        """上次异常退出（闪退）时 crash.log 会留下 traceback；启动时在日志区展示原因
        （用户不看控制台，诊断必须进 UI）。展示一次后改名 .old，避免每次启动重复刷屏。"""
        try:
            if not os.path.exists(CRASH_LOG) or os.path.getsize(CRASH_LOG) == 0:
                return
            with open(CRASH_LOG, "r", encoding="utf-8", errors="replace") as f:
                text = f.read().strip()
            if not text:
                return
            self.log_msg("[错误] 检测到上次运行异常退出（闪退），记录的原因如下：")
            for ln in text.splitlines()[-30:]:
                self.log_msg(ln if ln.strip() else "…")
            self.log_msg("[警告] 若当时散开/撤回被中断，请先检查文件是否完整再继续操作。")
            try:
                os.replace(CRASH_LOG, CRASH_LOG + ".old")
            except OSError:
                pass
        except Exception:
            pass

    # ---------- [备份] 提示联动 ----------
    def _sync_backup_notice_log(self):
        """备份/撤回栈变动后同步日志里的 [备份] 提示：
        没备份了 → 删光全部 [备份] 行；还有 → 按最新统计原地重写。"""
        try:
            doc = self.log.document()
            positions, first_pos = [], None
            blk = doc.firstBlock()
            while blk.isValid():
                if blk.text().startswith("[备份]"):
                    if first_pos is None:
                        first_pos = blk.position()
                    positions.append(blk.position())
                blk = blk.next()
            if not positions:
                return
            # 从后往前删，前面删掉的位置才不会影响后面的
            for pos in reversed(positions):
                cur = QTextCursor(doc)
                cur.setPosition(pos)
                # 连行尾换行一起选中删掉，避免残留空行；最后一行选不动就只删本行
                if not cur.movePosition(QTextCursor.MoveOperation.NextBlock,
                                        QTextCursor.MoveMode.KeepAnchor):
                    cur.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                                     QTextCursor.MoveMode.KeepAnchor)
                cur.removeSelectedText()
            dirs = self._collect_backup_dirs_cb()
            if not dirs:
                return
            total = sum(dirs.values())
            lines = [f"[备份] 检测到 {total} 个被覆盖旧文件的备份"
                     f"（各盘根下「散开备份」目录，共 {len(dirs)} 处）："]
            lines += [f"[备份] {d}（{n} 个文件）"
                      for d, n in list(dirs.items())[:5]]
            lines.append("[备份] 点撤回会自动还原它们；确认不需要后可手动删除，"
                         "或用设置里的「清理失效备份」。")
            cur = QTextCursor(doc)
            cur.setPosition(first_pos)  # 原位重写，不追加到日志末尾
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(self._log_fg))
            for line in lines[:-1]:
                cur.insertText(line + "\n", fmt)
            cur.insertText(lines[-1], fmt)
        except Exception:
            pass

    def _show_backup_notice(self):
        """启动时汇总仍存在于磁盘的覆盖备份（各盘根下可见的「散开备份」目录），
        把路径贴进日志区，说明撤回自动还原 / 不需要可手动删。"""
        try:
            dirs = self._collect_backup_dirs_cb()
            if not dirs:
                return
            total = sum(dirs.values())
            self.log_msg(f"[备份] 检测到 {total} 个被覆盖旧文件的备份"
                         f"（各盘根下「散开备份」目录，共 {len(dirs)} 处）：")
            for d, n in list(dirs.items())[:5]:
                self.log_msg(f"[备份] {d}（{n} 个文件）")
            self.log_msg("[备份] 点撤回会自动还原它们；确认不需要后可手动删除，"
                         "或用设置里的「清理失效备份」。")
        except Exception:
            pass
