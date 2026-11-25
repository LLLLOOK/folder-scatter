"""App 拆分子模块：事件处理（悬停状态栏/全局拖拽/日志贴底跟随/树外点击取消选中）
（自 ui/main_window.py 逐字搬移，行为冻结）。

EventsMixin 无任何 __init__，全部方法仍写 self（经 App 多继承解析），调用面零改动；
super() 调用经 MRO 落到 QMainWindow，与原行为一致。
"""

import os

from PyQt6.QtWidgets import (
    QAbstractButton,
    QSpinBox,
)

from PyQt6.QtCore import (
    QEvent,
)

from PyQt6.QtGui import (
    QTextCursor,
)

from core.guards import _is_protected_system_dir


class EventsMixin:
    # ---------- 状态栏：悬停提示 ----------
    def eventFilter(self, obj, event):
        # 凡给控件加 tooltip 必须 installEventFilter 才能同步到状态栏
        if (isinstance(obj, QAbstractButton) or isinstance(obj, QSpinBox)) \
                and obj.toolTip() and event.type() == QEvent.Type.Enter:
            # 状态栏单行显示：换行替换为空格；超长截断，避免把窗口顶宽
            text = obj.toolTip().replace("\n", " ")
            if len(text) > 60:
                text = text[:60] + "…"
            self.status_label.setText(text)
        elif (isinstance(obj, QAbstractButton) or isinstance(obj, QSpinBox)) \
                and obj.toolTip() and event.type() == QEvent.Type.Leave:
            text = obj.toolTip().replace("\n", " ")
            if len(text) > 60:
                text = text[:60] + "…"
            if self.status_label.text() == text:
                self.status_label.setText("鼠标悬停选项可查看说明；可直接把文件夹拖入路径框")
        return super().eventFilter(obj, event)

    # ---------- 主窗口全局拖拽（任意空白区域拖入文件夹） ----------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
            if any(os.path.isdir(p) or os.path.isfile(p) for p in paths):
                event.acceptProposedAction()
                return
        event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        paths = [u.toLocalFile() for u in urls if u.isLocalFile()]
        if not paths:
            event.ignore()
            return
        dirs = [p for p in paths if os.path.isdir(p)]
        if dirs:
            target = dirs[0]
            multi = len(dirs) > 1
        else:
            target = os.path.dirname(paths[0])  # 拖入的是文件 → 用其所在文件夹
            multi = False
        # 磁盘根目录保护：只把路径填入输入框，绝不扫描/加载目录树，直接 return
        if self._is_root_dir(target):
            self.entry_path.setText(target)
            self._show_root_warning()
            self.status_label.setText(
                f"已检测到磁盘根目录：{target}，请勿刷新或散开。")
            event.acceptProposedAction()
            return
        # 系统关键目录保护：只填入，绝不加载/散开
        if _is_protected_system_dir(target):
            self.entry_path.setText(target)
            self._show_system_warning()
            self.status_label.setText(
                f"已检测到系统关键目录：{target}，禁止刷新或散开。")
            event.acceptProposedAction()
            return
        self.entry_path.setText(target)
        self._save_parent_dir()  # 父目录变更 → 记忆设置
        self.root_warning_label.setVisible(False)
        self.refresh()
        if multi:
            self.status_label.setText("已选择第一个文件夹作为父目录，忽略了其余项。")
        event.acceptProposedAction()

    # ---------- 窗口拉伸时锁定日志滚动条到底部 ----------
    def resizeEvent(self, event):
        """窗口拉宽/拉高时，文本重排会让滚动条跳动；仅当原本贴着底部时才把
        光标重新锚到末尾跟随最新日志——正在上翻阅读旧日志/错误时不被打扰。"""
        super().resizeEvent(event)
        if hasattr(self, "log") and self.log is not None:
            _sb = self.log.verticalScrollBar()
            if _sb.maximum() - _sb.value() <= 4:
                self.log.moveCursor(QTextCursor.MoveOperation.End)
                self.log.ensureCursorVisible()

    # ---------- 点击目录树之外的区域时取消蓝色高亮选中 ----------
    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        # 仅当点击位置落在目录树范围之外（空白区/高级筛选/顶部按钮等）才取消选中
        local = self.tree.mapFromGlobal(event.globalPosition().toPoint())
        if not self.tree.rect().contains(local):
            self.tree.clearSelection()
