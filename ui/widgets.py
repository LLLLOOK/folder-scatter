import os

from PyQt6.QtWidgets import QTreeWidget, QTreeWidgetItem

from PyQt6.QtCore import QFileInfo, QTimer, Qt

from PyQt6.QtGui import QBrush, QColor
from infra.fileutil import natural_sort_key
from infra.paths import BACKUP_DIR_NAME
from core.guards import _is_link_or_junction


# ---------- 支持 Shift 区间勾选的目录树 ----------
class CheckTreeWidget(QTreeWidget):
    """在普通目录树基础上增加 Shift 区间勾选：

    先点一个文件夹作锚点，再按住 Shift 点另一个文件夹——点整行任意位置即可
    （文字、图标、勾选框都行，不限于勾选框），把两者之间按可见顺序（含两端）
    的文件夹全部勾上。

    自定义逻辑建立在默认鼠标处理之上（始终调用 super()），让 Qt 正常维护
    「当前项/选中锚点」等视图状态，避免选中项渲染异常、勾选框失灵。
    点击展开箭头、点击文字、点勾选框仍走默认行为（展开/选中/切换）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._anchor_item = None  # Shift 区间勾选的锚点（上次普通点击的文件夹）

    @staticmethod
    def _is_folder(item):
        d = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        return bool(d and d.get("is_dir"))

    def _visible_folder_items(self):
        """按可见顺序（自上而下，尊重折叠状态）收集所有文件夹项。"""
        result = []
        first = None
        for i in range(self.topLevelItemCount()):
            cand = self.topLevelItem(i)
            if not cand.isHidden():
                first = cand
                break
        it = first
        while it is not None:
            d = it.data(0, Qt.ItemDataRole.UserRole)
            if d and d.get("is_dir"):
                result.append(it)
            it = self.itemBelow(it)
        return result

    def _range_check(self, target_item):
        """Shift+点击：把锚点到目标之间（含两端）的可见文件夹全部勾上。"""
        visible = self._visible_folder_items()
        anchor = self._anchor_item
        if anchor is None or anchor not in visible or target_item not in visible:
            target_item.setCheckState(0, Qt.CheckState.Checked)  # 无有效锚点：仅勾目标
            return
        i_a = visible.index(anchor)
        i_t = visible.index(target_item)
        lo, hi = (i_a, i_t) if i_a <= i_t else (i_t, i_a)
        for it in visible[lo:hi + 1]:
            it.setCheckState(0, Qt.CheckState.Checked)

    def _flash_range(self, a, b):
        """Shift 区间勾选的临时反馈：两端套用单选同款选中描边高亮，
        中间行经 drawRow 自绘与 ::item:hover 同色的无边框高亮
        （QSS 接管 item 绘制后 setBackground 不生效，故自绘）；
        0.8 秒后全部清除，只留勾选。"""
        if a is None or b is None:
            return
        visible = self._visible_folder_items()
        if a not in visible or b not in visible:
            return
        lo, hi = sorted((visible.index(a), visible.index(b)))
        self.clearSelection()
        self._flash_middles = []  # QTreeWidgetItem 不可哈希，用列表
        span = visible[lo:hi + 1]
        for i, it in enumerate(span):
            if i == 0 or i == len(span) - 1:
                it.setSelected(True)  # 两端：单选同款描边高亮
            else:
                self._flash_middles.append(it)
        self.viewport().update()

        def _clear():
            self._flash_middles = []
            self.clearSelection()
            self.viewport().update()

        # 控件子 QTimer（随窗口一起销毁）：不用裸 singleShot——窗口若在 0.8 秒内
        # 销毁，裸 singleShot 的回调会打到已删除的 C++ 对象上
        t = getattr(self, "_flash_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(_clear)
            self._flash_timer = t
        t.start(800)

    def drawRow(self, painter, options, index):
        super().drawRow(painter, options, index)
        item = self.itemFromIndex(index)
        if item is not None and item in getattr(self, "_flash_middles", ()):
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(128, 128, 128, 38)))
            painter.drawRect(options.rect)
            painter.restore()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.pos())
            if self._is_folder(item):
                # Shift+点击整行任意位置：区间勾选。先勾好区间，再临时摘掉勾选标志走默认
                # 流程（让 Qt 更新当前项/选中高亮等状态，但不会再切换该勾选框造成重复）
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self._range_check(item)
                    flags = item.flags()
                    item.setFlags(flags & ~Qt.ItemFlag.ItemIsUserCheckable)
                    super().mousePressEvent(event)
                    item.setFlags(flags)
                    self._flash_range(self._anchor_item, item)
                    return
                # 普通点击：设为锚点，走默认（点勾选框切换 / 点文字选中 / 点箭头展开）
                self._anchor_item = item
        super().mousePressEvent(event)


def _list_entries(path):
    """列出 path 下的子目录与文件，按自然排序。模块级，供 TreeScanWorker 与增量刷新共用。"""
    try:
        entries = [e for e in os.listdir(path) if e != BACKUP_DIR_NAME]
    except OSError:
        return [], []
    dirs = sorted([e for e in entries if os.path.isdir(os.path.join(path, e))],
                  key=natural_sort_key)
    files = sorted([e for e in entries if os.path.isfile(os.path.join(path, e))],
                   key=natural_sort_key)
    return dirs, files


def build_tree_item(name, parent_path, icon_provider, stop=None, depth=0):
    """递归构建某个节点的完整子树（子文件夹 + 文件），全部离线完成。
    返回已 attach 好所有子节点的 QTreeWidgetItem。供 TreeScanWorker 与增量刷新共用。
    stop: 可选中止回调，返回 True 时尽快停止深入（刷新被新一轮取代时让旧线程早退）；
    depth: 当前递归深度，达 MAX_DEPTH 不再深入（防异常嵌套/近爆栈路径）。"""
    MAX_DEPTH = 100
    full = os.path.join(parent_path, name)
    item = QTreeWidgetItem()
    item.setText(0, name)
    is_dir = os.path.isdir(full)
    item.setData(0, Qt.ItemDataRole.UserRole, {"path": full, "is_dir": is_dir})
    if is_dir:
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, Qt.CheckState.Unchecked)
        item.setIcon(0, icon_provider.icon(QFileInfo(full)))
        # 链接/junction/挂载点不穿透（显示为叶子节点）：否则循环链接会把递归绕死，
        # 且树里展开的内容与散开实际处理的范围不一致；超深/已中止同样不再深入
        if (not _is_link_or_junction(full) and depth < MAX_DEPTH
                and not (stop is not None and stop())):
            sub_dirs, sub_files = _list_entries(full)
            for child_name in sub_dirs + sub_files:
                if stop is not None and stop():
                    break
                item.addChild(build_tree_item(child_name, full, icon_provider,
                                              stop=stop, depth=depth + 1))
    else:
        item.setIcon(0, icon_provider.icon(QFileInfo(full)))
    return item
