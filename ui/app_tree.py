"""App 拆分子模块：目录树勾选/增量刷新/浏览（自 ui/main_window.py 逐字搬移，行为冻结）。

TreeOpsMixin 无任何 __init__，全部方法仍写 self（经 App 多继承解析），调用面零改动。
"""

import os

from PyQt6.QtWidgets import (
    QFileDialog,
    QFileIconProvider,
)

from PyQt6.QtCore import (
    Qt,
)

from infra.fileutil import natural_sort_key
from ui.widgets import build_tree_item


class TreeOpsMixin:
    def _update_tree_hint(self):
        """目录树为空时切到空状态引导页，非空时切回目录树。"""
        if hasattr(self, "tree_stack"):
            empty = self.tree.topLevelItemCount() == 0
            self.tree_stack.setCurrentIndex(1 if empty else 0)

    def clear_tree(self):
        """清空目录：父目录输入框与目录树一并清空（不删除任何文件）。
        想重新扫描同一目录直接按「刷新」（refresh 本身就会清树重建）。"""
        # 在途扫描的滞留批次会把刚清空的树重新填回来，先中止并作废其代际
        self._abort_active_scan()
        self.root_warning_label.setVisible(False)  # 清空目录时同步消掉系统/根目录红色预警
        # 跳过统计黄字（大文件/占用/权限）一并清掉——目录都清了，上次任务的警示没有意义
        self._skip_counts = (0, 0, 0)
        if hasattr(self, "skip_label"):
            self.skip_label.setText("")
            self.skip_label.setVisible(False)
        self.entry_path.clear()
        self.tree.clear()
        # 进度条同步回空态：目录都清了，残留的上次任务进度没有意义
        self.progress.setMaximum(100)
        self.progress.setValue(0)
        self.progress.setFormat("")
        if hasattr(self, "folder_items"):
            self.folder_items.clear()
        self._update_tree_hint()  # 树已清空 → 显示空状态引导页

    def apply_default_checks(self):
        """默认勾选顶层（父级）文件夹。散开深度不再参与勾选，故与 level 无关——
        深度只在散开时由引擎按层展开勾选的文件夹（见 _expand_selected_by_depth）。"""
        base = self.parent_dir
        base_depth = base.count(os.sep) if base else 0
        for item in self.folder_items:
            depth = item.data(0, Qt.ItemDataRole.UserRole)["path"].count(os.sep) - base_depth
            item.setCheckState(0, Qt.CheckState.Checked if depth == 1 else Qt.CheckState.Unchecked)

    def _partial_refresh(self, selected):
        """散开后局部更新目录树（替代全树 refresh）：

        - 删除已散开并清空删除的源文件夹节点
        - 仅把磁盘上『树里还没有』的父目录直接子项增量加入（递归建子树）
        - 未选中的其他子树保持原样，不重扫、不重取图标

        相比 refresh() 的全树递归重建，本方法只触碰『被散开的源』与『散开新增的顶层项』，
        在『只散开部分子文件夹』的场景下能省掉大量未改动子树的 listdir + 取图标开销。
        （极端场景——所有子文件夹都被选中散开、文件全堆到父目录顶层——节点总数不变，
         此时收益主要体现在『不重扫未改动部分』；而部分散开场景收益最明显。）
        """
        base = self.parent_dir
        if not base or not os.path.isdir(base):
            return

        # 1) 删除已不存在于磁盘的源文件夹节点（散开 move 后变空被 rmdir）。
        #    嵌套勾选（二级及以下文件夹）时源节点不是顶层项——只清顶层会留
        #    幽灵节点（对已不存在的目录再操作必报错），须沿整棵树递归剪掉
        rm = set(os.path.normpath(p) for p in (selected or []))

        def _prune(parent_item):
            for ci in range(parent_item.childCount() - 1, -1, -1):
                child = parent_item.child(ci)
                d = child.data(0, Qt.ItemDataRole.UserRole)
                if (d and d.get("is_dir") and os.path.normpath(d["path"]) in rm
                        and not os.path.exists(d["path"])):
                    parent_item.removeChild(child)
                else:
                    _prune(child)

        _prune(self.tree.invisibleRootItem())

        # 2) 收集树中现有父目录顶层 path
        existing = set()
        for i in range(self.tree.topLevelItemCount()):
            d = self.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
            if d:
                existing.add(os.path.normpath(d["path"]))

        # 3) 读磁盘父目录直接子项，增量补齐缺失项（仅对新增项递归建子树 + 取图标）
        try:
            entries = os.listdir(base)
        except OSError:
            entries = []
        missing = [name for name in sorted(entries, key=natural_sort_key)
                   if os.path.normpath(os.path.join(base, name)) not in existing]
        # 新增项过多（如大散开后父目录涌入数千文件）：GUI 线程建树+取图标会卡界面数秒，
        # 回退为全量异步刷新（TreeScanWorker 在子线程建树，与本方法的小增量场景互补）
        if len(missing) > 300:
            self.refresh()
            return
        icon_provider = QFileIconProvider()
        new_items = []
        for name in missing:
            new_items.append(build_tree_item(name, base, icon_provider))

        if new_items:
            self.tree.setUpdatesEnabled(False)
            # 新散出的内容置顶显示：按自然排序依次插到最前（下次全量刷新恢复全局排序）
            for _i, _ni in enumerate(new_items):
                self.tree.insertTopLevelItem(_i, _ni)
            self.tree.setUpdatesEnabled(True)

        self._collect_folder_items()
        self._update_tree_hint()  # 增量刷新后树可能已被抽空（全部散开）→ 按需显示引导页
        # 散开后不再自动按 level 模式勾选，避免覆盖用户之前的勾选状态
        # self.apply_default_checks()

    def check_all(self):
        """全选父级：只勾顶层（父级）文件夹，深层子文件夹一律不勾。"""
        base = self.parent_dir
        base_depth = base.count(os.sep) if base else 0
        for it in self.folder_items:
            d = it.data(0, Qt.ItemDataRole.UserRole)
            if d and d.get("is_dir"):
                depth = d["path"].count(os.sep) - base_depth
                it.setCheckState(0, Qt.CheckState.Checked if depth == 1 else Qt.CheckState.Unchecked)

    def uncheck_all(self):
        """取消全选：递归遍历目录树里的每一个项目（顶层/二级/三级……所有文件夹），
        将其勾选状态强制设为未勾选（Qt.Unchecked）。
        不读取、不依赖任何父子三态逻辑，逐一对每个 item 直接赋值，确保彻底清空。"""

        def walk(item):
            # 只对文件夹取消勾选；文件本无勾选框，若对其 setCheckState 会硬显示出一个钩子
            d = item.data(0, Qt.ItemDataRole.UserRole)
            if d and d.get("is_dir"):
                item.setCheckState(0, Qt.CheckState.Unchecked)
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))

    def expand_all(self):
        for it in self.folder_items:
            it.setExpanded(True)

    def collapse_all(self):
        def rec(it):
            it.setExpanded(False)
            for i in range(it.childCount()):
                rec(it.child(i))
        for i in range(self.tree.topLevelItemCount()):
            rec(self.tree.topLevelItem(i))

    def toggle_expand(self):
        """展开全部 / 折叠全部 合并切换：按钮文字随状态变化。"""
        if self.btn_expand_toggle.text() == "展开全部":
            self.expand_all()
            self.btn_expand_toggle.setText("折叠全部")
        else:
            self.collapse_all()
            self.btn_expand_toggle.setText("展开全部")

    def browse(self):
        d = QFileDialog.getExistingDirectory(self, "选择父目录", self.entry_path.text())
        if d:
            self.entry_path.setText(d)
            self._save_parent_dir()  # 父目录变更 → 记忆设置
            self.refresh()
