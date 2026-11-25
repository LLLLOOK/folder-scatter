"""App 拆分子模块：界面构建（_build_*）与 DropLineEdit（自 ui/main_window.py 逐字搬移，行为冻结）。

BuildMixin 无任何 __init__，全部方法仍写 self（经 App 多继承解析），调用面零改动。
DropLineEdit.dropEvent 里的 App 静态根目录判定改为方法内惰性 import，避免与
ui/main_window.py 循环依赖；调用本身保持逐字。
"""

import os

from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from PyQt6.QtCore import (
    QSize,
    Qt,
)

from PyQt6.QtGui import (
    QKeySequence,
    QShortcut,
)

from infra.logutil import LOG_MAX_BLOCKS
from core.guards import _is_protected_system_dir
from ui.icons import _emoji_icon
from ui.style import DARK_BG, LIGHT_BG, _translate_menu, scrollbar_qss
from ui.widgets import CheckTreeWidget


class DropLineEdit(QLineEdit):
    """支持从资源管理器拖拽文件夹进入的 QLineEdit。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._main = None  # 指向 App，用于刷新目录树 + 状态栏提示

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        from ui.main_window import App  # 惰性 import：避免与本模块循环依赖
        urls = event.mimeData().urls()
        paths = [u.toLocalFile() for u in urls if u.isLocalFile()]
        valid_dirs = [p for p in paths if os.path.isdir(p)]
        if not valid_dirs:
            if self._main is not None:
                self._main.status_label.setText("拖入的内容不是有效文件夹")
            event.ignore()
            return
        target = valid_dirs[0]
        # 磁盘根目录保护：只把路径填入输入框，绝不扫描/加载目录树，直接 return
        if App._is_root_dir(target):
            self.setText(target)
            if self._main is not None:
                self._main._show_root_warning()
                self._main.status_label.setText(
                    f"已检测到磁盘根目录：{target}，请勿刷新或散开。")
            event.acceptProposedAction()
            return
        # 系统关键目录保护：只填入，绝不加载/散开
        if _is_protected_system_dir(target):
            self.setText(target)
            if self._main is not None:
                self._main._show_system_warning()
                self._main.status_label.setText(
                    f"已检测到系统关键目录：{target}，禁止刷新或散开。")
            event.acceptProposedAction()
            return
        self.setText(target)
        if self._main is not None:
            self._main.root_warning_label.setVisible(False)
            self._main.refresh()
            if len(valid_dirs) > 1:
                self._main.status_label.setText(
                    "已选择第一个文件夹作为父目录，忽略了其余项。")
            else:
                self._main.status_label.setText(f"已载入：{target}")
        event.acceptProposedAction()

    def contextMenuEvent(self, event):
        """右键菜单保留 Qt 标准样式（图标/快捷键），仅将英文文字替换为中文。"""
        menu = self.createStandardContextMenu()
        _translate_menu(menu)
        menu.exec(event.globalPos())


class BuildMixin:
    # ---------- 构建界面 ----------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        # 小屏保护：整块界面包进 QScrollArea，窗口小于内容最小尺寸时可滚动查看，不再裁切
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(16, 16, 16, 12)  # 上 16px；底部留 12px 呼吸感，状态栏不贴边
        root.setSpacing(0)  # 区块间距改为手动 addSpacing 精确控制
        scroll.setWidget(content)
        cl = QVBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(scroll)

        self._build_top_bar(root)
        self._build_options(root)
        # 目录树占满剩余高度；底部「操作栏 + 日志」固定高度贴底、不参与拉伸。
        # 不再用上下分隔器：拖动分隔条会带着日志一起上下跑，滚目录树时也很难
        # 保持日志在原位（用户要求日志永远贴在底部不动）
        root.addWidget(self._build_tree_area(root), 1)

        # 下半部分容器：进度条 + 操作按钮行 + 日志框（垂直堆叠）
        bottom = QWidget()
        bv = QVBoxLayout(bottom)
        bv.setContentsMargins(0, 6, 0, 6)
        bv.setSpacing(8)
        self._build_action_bar(bv)
        self._build_log_area(bv)
        root.addWidget(bottom)

        # 状态栏：使用 QLabel 替代 QStatusBar，便于控制边距（左对齐内容、底部 12px）
        self.status_label = QLabel("鼠标悬停选项可查看说明；可直接把文件夹拖入路径框")
        self.status_label.setObjectName("statusLabel")
        root.addWidget(self.status_label)
        self.setStatusBar(None)  # 禁用主窗口默认状态栏，避免双层状态栏
        # 扫描调度所需控件全部就绪（entry_path/progress/root_warning_label/tree/
        # status_label/log），移交引用；此后 refresh 等路径才会被触发
        self.scan_ctl.attach(self.entry_path, self.progress,
                             self.root_warning_label, self.tree,
                             self.status_label, self.log)

        # 快捷键：F5 刷新、Ctrl+Enter 开始散开；Ctrl+A 全选仅作用于目录树
        # （WidgetShortcut 上下文，不抢路径框/日志框原生的文本全选）
        QShortcut(QKeySequence("F5"), self, self.refresh)
        QShortcut(QKeySequence("Ctrl+Return"), self, self.start)
        _sc_all = QShortcut(QKeySequence.StandardKey.SelectAll, self.tree, self.check_all)
        _sc_all.setContext(Qt.ShortcutContext.WidgetShortcut)

        self._update_tree_hint()

    def _build_top_bar(self, root):
        # 顶部：父目录 + 按钮 + 主题切换
        top = QHBoxLayout()
        top.setSpacing(6)  # 与目录树工具栏按钮间距一致
        top.addWidget(QLabel("父目录："))
        self.entry_path = DropLineEdit()
        self.entry_path.setPlaceholderText("粘贴目录路径，或点击浏览选择...（支持拖拽文件夹）")
        self.entry_path.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.entry_path._main = self
        # 粘贴/输入路径后按回车（Enter）即加载目录树，效果等同于点“刷新”
        self.entry_path.returnPressed.connect(self.refresh)
        top.addWidget(self.entry_path, 1)  # 跟随窗口拉宽，多余空间优先给输入框
        btn_browse = QPushButton("浏览...")
        btn_browse.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        btn_browse.setToolTip("浏览并选择父目录")
        btn_browse.installEventFilter(self)
        btn_browse.clicked.connect(self.browse)
        top.addWidget(btn_browse)
        self.btn_open_dir = QPushButton("打开目录")
        self.btn_open_dir.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.btn_open_dir.setToolTip("在资源管理器中打开当前父目录")
        self.btn_open_dir.installEventFilter(self)
        self.btn_open_dir.clicked.connect(self.open_parent_dir)
        top.addWidget(self.btn_open_dir)
        btn_refresh = QPushButton("刷新")
        btn_refresh.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        btn_refresh.setToolTip("重新扫描当前父目录（F5；修改路径后按回车同效）")
        btn_refresh.installEventFilter(self)
        btn_refresh.clicked.connect(self.refresh)
        top.addWidget(btn_refresh)
        self.btn_clear_tree = QPushButton("清空目录")
        self.btn_clear_tree.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.btn_clear_tree.setToolTip("清空当前目录树与勾选项（不删除任何文件）")
        self.btn_clear_tree.installEventFilter(self)
        self.btn_clear_tree.clicked.connect(self.clear_tree)
        top.addWidget(self.btn_clear_tree)
        self.theme_btn = QPushButton()
        self.theme_btn.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        # 只覆盖 padding：左 4 右 6（图标字形自带左 bearing，左移 2px 视觉居中）
        self.theme_btn.setStyleSheet("QPushButton { padding: 5px 6px 5px 4px; }")
        self.theme_btn.installEventFilter(self)  # tooltip 由 apply_theme 动态设置
        self.theme_btn.clicked.connect(self.toggle_theme)
        top.addWidget(self.theme_btn)
        # 设置按钮（⚙️）：放在暗色切换按钮右边；父目录输入框为 Expanding，
        # 多出的按钮会自动挤压输入框长度，无需额外处理。
        self.btn_settings = QPushButton("设置")
        self.btn_settings.setIcon(_emoji_icon("⚙️", 16))
        self.btn_settings.setIconSize(QSize(14, 14))
        self.btn_settings.setStyleSheet("QPushButton { padding: 5px 6px 5px 2px; }")
        self.btn_settings.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.btn_settings.setToolTip("软件通用设置（首选项）")
        self.btn_settings.installEventFilter(self)
        self.btn_settings.clicked.connect(self.open_settings)
        top.addWidget(self.btn_settings)
        root.addLayout(top)
        root.addSpacing(9)

    def _build_options(self, root):
        # 选项分组（散开模式 / 操作方式 / 冲突处理）—— 保持不变
        opt = QHBoxLayout()
        opt.setSpacing(8)

        self.level_group = QButtonGroup(self)
        grp_level = QGroupBox("散开深度")
        grp_level.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        gl = QVBoxLayout(grp_level)
        gl.setContentsMargins(9, 0, 9, 9)
        for text, val, tip in [
            ("仅当前层散开", "one", "仅将当前目录下的文件提取到根目录，子文件夹保持不动。"),
            ("包含子文件夹散开", "two", "连同直接子文件夹内的文件一起，提取到根目录。"),
            ("全部层级散开", "all", "无视文件夹层级，将内部所有嵌套的文件全部提取到根目录。"),
        ]:
            rb = QRadioButton(text)
            rb.setToolTip(tip)
            rb.installEventFilter(self)
            rb.setProperty("optval", val)
            rb.toggled.connect(lambda checked, v=val: self._set_level(checked, v))
            self.level_group.addButton(rb)
            if val == "one":
                rb.setChecked(True)
            gl.addWidget(rb)
        opt.addWidget(grp_level, 1)

        self.mode_group = QButtonGroup(self)
        grp_mode = QGroupBox("操作方式")
        grp_mode.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        gm = QVBoxLayout(grp_mode)
        gm.setContentsMargins(9, 0, 9, 9)
        for text, val, tip in [
            ("移动（剪切）", "move", "文件剪切到目标位置，原位置不再保留。"),
            ("复制", "copy", "保留原文件，在目标位置生成副本。"),
        ]:
            rb = QRadioButton(text)
            rb.setToolTip(tip)
            rb.installEventFilter(self)
            rb.setProperty("optval", val)
            rb.toggled.connect(lambda checked, v=val: self._set_mode(checked, v))
            self.mode_group.addButton(rb)
            if val == "move":
                rb.setChecked(True)
            gm.addWidget(rb)
        opt.addWidget(grp_mode, 1)

        self.conflict_group = QButtonGroup(self)
        grp_conflict = QGroupBox("冲突处理")
        grp_conflict.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        gc = QVBoxLayout(grp_conflict)
        gc.setContentsMargins(9, 0, 9, 9)
        for text, val, tip in [
            ("覆盖", "overwrite", "如果目标目录存在同名文件，将直接覆盖并替换旧文件。"),
            ("跳过", "skip", "如果目标目录存在同名文件，将跳过该文件，保留原有文件。"),
            ("自动重命名 (后缀-1)", "rename", "自动重命名 (-1)：如果目标目录存在同名文件，将自动重命名新文件，在原文件名后添加 -1、-2 等后缀（例如：原名-1.png）。"),
        ]:
            rb = QRadioButton(text)
            rb.setToolTip(tip)
            rb.installEventFilter(self)
            rb.setProperty("optval", val)
            rb.toggled.connect(lambda checked, v=val: self._set_conflict(checked, v))
            self.conflict_group.addButton(rb)
            if val == "overwrite":
                # 「覆盖」独占一行；覆盖前备份作为其缩进子项独占下一行（层级关系一眼可见）
                gc.addWidget(rb)
                sub = QHBoxLayout()
                sub.setContentsMargins(24, 0, 0, 0)  # 与单选指示器对齐的缩进
                sub.setSpacing(6)
                self.chk_trash = QCheckBox("覆盖前备份旧文件（撤回可还原）")
                self.chk_trash.setToolTip(
                    "覆盖时旧文件移入「散开备份」，撤回自动还原。")
                self.chk_trash.installEventFilter(self)  # 悬停时状态栏同步显示 tooltip
                self.chk_trash.setContentsMargins(0, 0, 0, 0)
                self.chk_trash.setEnabled(False)
                # 回读保存值（默认勾选）：与右键静默模式读同一键，避免两边分岔
                self.chk_trash.setChecked(bool(self.settings.value(
                    "conflict_trash", True, type=bool)))
                self.chk_trash.toggled.connect(self._on_chk_trash_toggled)
                self._update_chk_trash_color()
                sub.addWidget(self.chk_trash)
                sub.addStretch(1)
                gc.addLayout(sub)
                rb.setChecked(True)   # 默认选中「覆盖」
            else:
                gc.addWidget(rb)
        opt.addWidget(grp_conflict, 1)
        root.addLayout(opt)
        root.addSpacing(9)

        # ---- 高级筛选区（第三行，QGroupBox 包裹，与上方分组框视觉统一） ----
        # 只提取：4 个平铺复选框 + 自定义后缀输入框；忽略体积：复选框门控数字框
        adv_grp = QGroupBox("高级筛选")
        adv_grp.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        adv = QHBoxLayout(adv_grp)
        adv.setContentsMargins(9, 0, 9, 9)  # 上边距 0（内容上贴标题，即「往上缩进 9」），左右 9px
        adv.setSpacing(8)
        adv.setAlignment(Qt.AlignmentFlag.AlignVCenter)  # 内部控件垂直居中

        # 后缀筛选模式切换按钮：点一下在「只散开」（白名单）与「只排除」（黑名单）间翻转，
        # 复用同一套后缀复选框 + 自定义输入框，含义随模式翻转（样式同「展开全部/折叠全部」）
        self.filter_exclude = bool(self.settings.value(
            "filter_exclude", False, type=bool))
        self.btn_filter_mode = QPushButton("只散开")
        self.btn_filter_mode.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.btn_filter_mode.setToolTip(
            "当前：只散开勾选的后缀（白名单）。\n点击切换为「只排除」：散开除勾选后缀外的所有文件。")
        self.btn_filter_mode.installEventFilter(self)
        self.btn_filter_mode.clicked.connect(self._toggle_filter_mode)
        self._apply_filter_mode()  # 按回读的模式设置文字与提示
        adv.addWidget(self.btn_filter_mode)
        self.suffix_checks = []
        for name in ["JPG", "PNG", "PSD", "C4D"]:
            cb = QCheckBox(name)
            cb.setChecked(False)
            adv.addWidget(cb)
            self.suffix_checks.append(cb)
        self.edit_custom_suffix = QLineEdit()
        self.edit_custom_suffix.setPlaceholderText("自定义后缀 (支持用空格或逗号隔开)")
        self.edit_custom_suffix.setMinimumWidth(60)  # 小屏可进一步压缩
        self.edit_custom_suffix.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.edit_custom_suffix.textChanged.connect(self._validate_custom_suffix)
        self.edit_custom_suffix.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.edit_custom_suffix.customContextMenuRequested.connect(self._show_suffix_context_menu)
        adv.addWidget(self.edit_custom_suffix, 1)  # 自定义框为唯一拉伸控件，自动占满中间剩余空间

        self.chk_ignore = QCheckBox("忽略大于此体积 (MB)")
        self.chk_ignore.setChecked(False)
        self.chk_ignore.toggled.connect(self._on_ignore_toggled)
        self.chk_ignore.setToolTip("0 代表不限制；大于该体积的文件将被跳过（黄色提示）。")
        self.spin_max = QSpinBox()
        self.spin_max.setRange(0, 99999)  # 最多 5 位数
        self.spin_max.setValue(500)
        self.spin_max.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 宽 = 5 位数字 + 左右各 6px 内边距 + 边框 + 4px 余量（不切字）
        self.spin_max.setFixedWidth(
            self.spin_max.fontMetrics().horizontalAdvance("99999") + 18)
        self.spin_max.setToolTip("0 代表不限制；大于该体积的文件将被跳过（黄色提示）。")
        self.spin_max.setEnabled(False)  # 默认禁用（灰色不可输入），勾选复选框后才启用
        self.spin_max.setStyleSheet(self._spin_style())  # 清晰边框（独立于全局 QSS，主题切换时重建）
        # 体积值变更即落盘：右键静默模式读同一键，须与界面实时一致
        self.spin_max.valueChanged.connect(self._on_ignore_size_changed)
        adv.addWidget(self.chk_ignore)
        adv.addWidget(self.spin_max)
        # 悬停同步到左下角状态栏（与单选按钮同机制）；复选框与数字框都装事件过滤器
        self.chk_ignore.installEventFilter(self)
        self.spin_max.installEventFilter(self)
        root.addWidget(adv_grp)
        root.addSpacing(16)  # 高级筛选 → 目录树 16px

    def _build_tree_area(self, root):
        # 树工具栏
        th = QHBoxLayout()
        th.setContentsMargins(0, 0, 0, 0)
        th.setSpacing(6)
        th.addWidget(QLabel("目录树（勾选文件夹以散开）"))

        # 跳过提示标签：放在"目录树（勾选文件夹以散开）"文字之后，
        # 与按钮同行，出现时不会把目录树整体框架往下挤。
        self.skip_label = QLabel("")
        self.skip_label.setStyleSheet(f"color: {self._log_warn};")
        self.skip_label.setVisible(False)
        th.addWidget(self.skip_label)

        # 磁盘根目录红色预警提示：拖入根目录（如 E:\）时显示，提示用户不要刷新/散开
        self.root_warning_label = QLabel("")
        self.root_warning_label.setStyleSheet(f"color: {self._log_error};")
        self.root_warning_label.setVisible(False)
        th.addWidget(self.root_warning_label)

        th.addStretch(1)
        for text, cmd, tip in [
            ("全选父级", self.check_all, "只勾选顶层（父级）文件夹，深层子文件夹不勾"),
            ("取消全选", self.uncheck_all, "取消所有文件夹的勾选"),
        ]:
            b = QPushButton(text)
            b.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            b.setToolTip(tip)
            b.clicked.connect(cmd)
            th.addWidget(b)

        # 展开/折叠合并为一个切换按钮：文字随状态变化
        self.btn_expand_toggle = QPushButton("展开全部")
        self.btn_expand_toggle.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.btn_expand_toggle.clicked.connect(self.toggle_expand)
        th.addWidget(self.btn_expand_toggle)

        root.addLayout(th)
        root.addSpacing(9)

        # 目录树（用支持左键拖拽框选勾选的子类）
        self.tree = CheckTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(1)
        self.tree.setStyleSheet(self._tree_style())

        self.tree_stack = QStackedWidget()
        self.tree_stack.addWidget(self.tree)  # 页 0：目录树本体
        hint = QLabel("尚未加载目录\n\n把文件夹拖入本窗口任意位置，"
                      "或点击「浏览…」选择父目录")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("color: #999999; font-size: 13px; background: transparent;")
        self.tree_stack.addWidget(hint)  # 页 1：空状态引导
        self.tree_stack.setCurrentIndex(1)  # 启动显示引导页

        # 目录树（含空状态引导页）直接交给根布局拉伸：底部「操作栏 + 日志」
        # 固定贴底，不再用上下分隔器（拖动时分隔条会带着日志一起上下跑）
        return self.tree_stack

    def _build_action_bar(self, bv):
        # 主操作栏（进度 + 按钮：模拟运行 → 开始散开 → 停止 → 撤回 → 清空日志，按钮靠右）
        act = QHBoxLayout()
        act.setSpacing(6)  # 与上方目录树工具栏按钮间距一致
        act.addWidget(QLabel("进度："))
        self.progress = QProgressBar()
        self.progress.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.progress.setMinimumWidth(40)
        self.progress.setFormat("")  # 空闲态不显示百分比文本
        act.addWidget(self.progress, 1)
        self.btn_simulate = QPushButton("模拟运行")
        self.btn_simulate.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.btn_simulate.setToolTip("只预览将要处理的文件清单，不产生任何实际改动")
        self.btn_simulate.installEventFilter(self)
        self.btn_simulate.clicked.connect(self.simulate)
        act.addWidget(self.btn_simulate)
        self.btn_start = QPushButton("开始散开")
        self.btn_start.setObjectName("accentButton")
        self.btn_start.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.btn_start.setToolTip("开始散开勾选的文件夹（Ctrl+Enter）")
        self.btn_start.installEventFilter(self)
        self.btn_start.clicked.connect(self.start)
        act.addWidget(self.btn_start)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.btn_stop.setToolTip("中止当前任务（已处理的文件保持散开后的状态）")
        self.btn_stop.installEventFilter(self)
        self.btn_stop.clicked.connect(self.stop_run)
        self.btn_stop.setEnabled(False)
        act.addWidget(self.btn_stop)
        self.btn_undo = QPushButton("撤回")
        self.btn_undo.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.btn_undo.setToolTip(
            "撤销上一次散开（最多20步）：移动文件放回原位，被覆盖旧文件从备份自动还原；复制副本送回收站。")
        self.btn_undo.installEventFilter(self)
        self.btn_undo.clicked.connect(self.undo)
        self.btn_undo.setEnabled(False)
        act.addWidget(self.btn_undo)
        # 撤回栈先清空；实际载入放到日志控件创建之后（见下方），避免 log_msg 在 self.log 未就绪时崩溃
        self.undo_stack = []
        # 日志相关的次要操作（清空/复制错误/导出）合并为一个下拉菜单，减少底部按钮数量
        self.btn_log_menu = QPushButton("日志 ▾")
        self.btn_log_menu.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.btn_log_menu.setToolTip("日志相关操作：清空 / 复制错误 / 导出")
        self.btn_log_menu.installEventFilter(self)
        # 隐藏 Qt 自动绘制的下拉箭头，让"▾"作为文字的一部分紧挨"日志"
        self.btn_log_menu.setStyleSheet("QPushButton::menu-indicator { image: none; width: 0px; }")
        log_menu = QMenu(self.btn_log_menu)
        # 去掉菜单窗口的原生边框与阴影，让样式表的圆角完整生效，避免圆角外叠出直角
        log_menu.setWindowFlags(log_menu.windowFlags() | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint)
        log_menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        act_clear = log_menu.addAction("清空日志")
        act_clear.setToolTip("清空下方日志内容")
        act_clear.triggered.connect(self.clear_log)
        act_copy = log_menu.addAction("复制错误")
        act_copy.setToolTip("将日志中全部 [错误] 行（及其后的 [建议]）复制到剪贴板，便于直接发给同事/求助。")
        act_copy.triggered.connect(self.copy_errors)
        act_export = log_menu.addAction("导出日志")
        act_export.setToolTip("把当前整份日志另存为 UTF-8(BOM) 文本文件，记事本打开不乱码。")
        act_export.triggered.connect(self.export_log)
        self.btn_log_menu.setMenu(log_menu)
        act.addWidget(self.btn_log_menu)
        bv.addLayout(act)

    def _build_log_area(self, bv):
        # 日志
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        _log_bg = DARK_BG if self.is_dark else LIGHT_BG
        _log_border = "#404040" if self.is_dark else "#cccccc"
        # 局部样式表必须带上滚动条规则：日志控件有自己的 QSS 时，滚动条子控件
        # 也在同一张表里取样式，漏写会退回系统默认的「带框凹槽」
        self.log.setStyleSheet(
            f"background-color: {_log_bg}; border: 1px solid {_log_border}; "
            "border-radius: 4px; font-family: Consolas, 'Microsoft YaHei', sans-serif;"
            + scrollbar_qss(self.is_dark))
        # 行数上限：超出自动丢弃最旧的（防超大任务日志无限驻留内存、越用越卡）；
        # 触顶提示标记在 log_msg 里首次置位，手动/自动清空日志后重置
        self.log.document().setMaximumBlockCount(LOG_MAX_BLOCKS)
        self._log_overflow_notified = False
        # 固定高度贴底：不随目录树/窗口拉伸而上下跑（用户要求日志永远在底部不动）
        self.log.setFixedHeight(200)
        bv.addWidget(self.log)
        self.log_panel.attach(self.log)  # 控件创建后交给 LogPanel 持有（UI 不动）

        # 启动时恢复上次的撤回历史：必须放在 self.log 创建之后，否则 log_msg 会因 self.log 未就绪而崩溃
        self._load_undo_history()
        self._load_history_log()
        self._apply_backup_retention()  # 按天保留清理（若开启）
        self._show_backup_notice()  # 现存备份汇总提示（若有）
