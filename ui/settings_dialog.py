import os
import json

from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from PyQt6.QtCore import (
    QSettings,
    QSize,
    QTimer,
    QUrl,
    Qt,
)

from PyQt6.QtGui import QDesktopServices
from win32.menu import _set_autostart
from core.guards import _is_protected_system_dir
from backup.vault import _count_backup_root, _iter_backup_roots
from ui.icons import _emoji_icon


class SettingsDialog(QDialog):
    """设置窗口：主题 / 右键菜单 / 启动与记忆 / 行为与体验 / 系统设置。
    内容包在 QScrollArea 内（小屏矮屏可滚动，底部按钮行固定不被顶出屏幕）。
    __init__ 只装配骨架，六大板块拆为 _build_*_section 方法。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(640)
        self._app = parent  # 主窗口 App 实例，用于读取当前操作配置
        # 外层：内容滚动区 + 固定底部按钮行
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        content = QWidget()
        layout = QVBoxLayout(content)
        # 上 16 与主界面一致；下 9：系统设置组下边框到按钮行总间距 6px
        layout.setContentsMargins(16, 10, 16, 9)
        layout.setSpacing(9)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        s = None
        if parent is not None and hasattr(parent, "settings"):
            s = parent.settings
        if s is None:
            s = QSettings("FolderPromoter", "FolderPromoter")

        self._build_theme_section(layout, s)
        self._build_menu_section(layout, s)
        self._build_backup_section(layout, s)
        self._build_memory_section(layout, s)
        self._build_behavior_section(layout, s)
        self._build_system_section(layout, s)
        layout.addStretch(1)  # 多余空间全部沉到底部：拉高窗口时各内容框保持自然高度不拉长

        btn_row = self._build_button_row()
        outer.addLayout(btn_row)

        # 初始尺寸：高度 = 刚好展示全部内容（超高屏幕放不下时回退滚动条）；
        # 宽度取 640 与内容自然宽的较大者
        content.adjustSize()
        need_h = content.sizeHint().height() + btn_row.sizeHint().height()
        max_h = QApplication.primaryScreen().availableGeometry().height() - 80
        self.resize(max(640, content.sizeHint().width()), min(need_h, max_h))

    # ---------- 板块〇：主题（跟随系统 / 亮色 / 暗色） ----------
    def _build_theme_section(self, layout, s):
        gb0 = QGroupBox("主题")
        h0 = QHBoxLayout(gb0)
        h0.setContentsMargins(9, 0, 9, 9)
        h0.setSpacing(12)
        self.theme_group = QButtonGroup(self)
        saved_theme = str(s.value("theme_mode", "dark"))  # 默认主题 = 深色
        for val, label in [
            ("system", "跟随系统"),
            ("light", "亮色"),
            ("dark", "暗色"),
        ]:
            rb = QRadioButton(label)
            rb.setProperty("optval", val)
            if val == saved_theme:
                rb.setChecked(True)
            self.theme_group.addButton(rb)
            h0.addWidget(rb)
        h0.addStretch(1)
        layout.addWidget(gb0)

    # ---------- 板块一：右键菜单设置（含默认操作方式 / 冲突处理） ----------
    def _build_menu_section(self, layout, s):
        gb1 = QGroupBox("右键菜单设置")
        h1 = QHBoxLayout(gb1)
        h1.setContentsMargins(9, 0, 9, 9)
        h1.setSpacing(12)

        # 左侧：菜单项勾选
        v1 = QVBoxLayout()
        v1.setSpacing(6)
        self.menu_checks = {}
        menu_options = [
            ("current", "在右键菜单显示“仅当前层散开”"),
            ("sub", "在右键菜单显示“包含子文件夹散开”"),
            ("all", "在右键菜单显示“全部层级散开”"),
            ("undo", "在右键菜单显示“在软件中打开（可撤回）”"),
        ]
        saved_menu = {}
        try:
            raw = s.value("menu_opts", "")
            if raw:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    saved_menu = loaded
                elif isinstance(loaded, list):
                    # 旧格式残留：只存勾选的 key 列表，按列表还原勾选态
                    saved_menu = {k: k in loaded for k, _ in menu_options}
                # 其他类型（数字/字符串等）一律回退默认
        except Exception:
            saved_menu = {}
        for key, label in menu_options:
            cb = QCheckBox(label)
            cb.setChecked(bool(saved_menu.get(key, True)))  # 默认全部勾选
            self.menu_checks[key] = cb
            v1.addWidget(cb)
        h1.addLayout(v1, 1)

        # 分隔线 1
        line1 = QFrame()
        line1.setFrameShape(QFrame.Shape.VLine)
        line1.setFrameShadow(QFrame.Shadow.Sunken)
        h1.addWidget(line1)

        # 中间：右键菜单默认操作方式
        v1_mode = QVBoxLayout()
        v1_mode.setSpacing(6)
        lbl_mode = QLabel("右键菜单默认操作方式")
        lbl_mode.setStyleSheet("font-weight: bold;")
        v1_mode.addWidget(lbl_mode)
        self.menu_mode_group = QButtonGroup(self)
        mode_options = [
            ("move", "移动（剪切）"),
            ("copy", "复制"),
        ]
        saved_mode = str(s.value("menu_mode", "move"))
        for val, label in mode_options:
            rb = QRadioButton(label)
            rb.setProperty("optval", val)
            if val == saved_mode:
                rb.setChecked(True)
            self.menu_mode_group.addButton(rb)
            v1_mode.addWidget(rb)
        v1_mode.addStretch(1)
        h1.addLayout(v1_mode, 1)

        # 分隔线 2
        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.VLine)
        line2.setFrameShadow(QFrame.Shadow.Sunken)
        h1.addWidget(line2)

        # 右侧：右键菜单默认冲突处理
        v1_right = QVBoxLayout()
        v1_right.setSpacing(6)
        lbl_conflict = QLabel("右键菜单默认冲突处理")
        lbl_conflict.setStyleSheet("font-weight: bold;")
        v1_right.addWidget(lbl_conflict)
        self.menu_conflict_group = QButtonGroup(self)
        conflict_options = [
            ("overwrite", "覆盖"),
            ("skip", "跳过"),
            ("rename", "自动重命名（后缀-1）"),
        ]
        saved_conflict = str(s.value("menu_conflict", "overwrite"))
        for val, label in conflict_options:
            rb = QRadioButton(label)
            rb.setProperty("optval", val)
            if val == saved_conflict:
                rb.setChecked(True)
            self.menu_conflict_group.addButton(rb)
            v1_right.addWidget(rb)
            if val == "overwrite":
                # 「覆盖」的缩进子项：与主界面同款，读写同一 conflict_trash 键；
                # 初始状态跟随主界面复选框当前值（主界面启动时回读同一设置键）
                self.chk_menu_trash = QCheckBox("覆盖前备份旧文件（撤回可还原）")
                main_trash = getattr(self._app, "chk_trash", None)
                self.chk_menu_trash.setChecked(
                    main_trash.isChecked() if main_trash is not None
                    else bool(s.value("conflict_trash", True, type=bool)))
                self.chk_menu_trash.setEnabled(saved_conflict == "overwrite")
                sub_conf = QHBoxLayout()
                sub_conf.setContentsMargins(24, 0, 0, 0)  # 与单选指示器对齐的缩进
                sub_conf.addWidget(self.chk_menu_trash)
                sub_conf.addStretch(1)
                v1_right.addLayout(sub_conf)
        self.menu_conflict_group.buttonToggled.connect(self._on_menu_conflict_toggled)
        v1_right.addStretch(1)
        h1.addLayout(v1_right, 1)

        layout.addWidget(gb1)

    # ---------- 板块：备份管理 ----------
    def _build_backup_section(self, layout, s):
        gbB = QGroupBox("备份管理")
        vB = QVBoxLayout(gbB)
        vB.setContentsMargins(9, 0, 9, 9)
        vB.setSpacing(6)
        _saved_custom = bool(s.value("backup_custom_enabled", False, type=bool))
        _saved_custom_path = str(s.value("backup_custom_path", "") or "").strip()
        # 各备份根目录现状 + 「打开备份」按钮（勾选自定义时灰显）。
        # 每行包一层 QWidget 注册进 _backup_rows，点「清空备份」后可整行移除。
        self._backup_layout = vB
        self._backup_rows = {}  # normcase(root) -> {row, lb, btn, btn2, path, tag}
        roots = _iter_backup_roots()
        self._backup_empty_lb = QLabel("（暂无备份：散开发生覆盖时才会产生备份）")
        vB.addWidget(self._backup_empty_lb)
        self._backup_empty_lb.setVisible(not roots)
        for r in roots:
            if _saved_custom_path and os.path.normcase(r) == \
                    os.path.normcase(_saved_custom_path):
                tag = "自定义"
            elif len(r) >= 2 and r[1] == ":":
                tag = f"{r[0]} 盘"
            else:
                tag = ""
            row = QWidget()
            hrow = QHBoxLayout(row)
            hrow.setContentsMargins(0, 0, 0, 0)
            lb = QLabel(self._backup_label_text(tag, r))
            btn = QPushButton("打开备份")
            btn.clicked.connect(lambda _c, p=r: self._open_backup_root(p))
            btn2 = QPushButton("清空备份")
            btn2.clicked.connect(
                lambda _c, p=r: self._empty_backups_clicked(p))
            hrow.addWidget(lb)
            hrow.addWidget(btn)
            hrow.addWidget(btn2)
            hrow.addStretch(1)
            vB.addWidget(row)
            self._backup_rows[os.path.normcase(r)] = {
                "row": row, "lb": lb, "btn": btn, "btn2": btn2,
                "path": r, "tag": tag}
        # 自定义备份目录（父级顶格；其下警告行缩进当子项）
        cust_row = QHBoxLayout()
        self.chk_backup_custom = QCheckBox("使用自定义备份目录")
        self.chk_backup_custom.setChecked(_saved_custom)
        cust_row.addWidget(self.chk_backup_custom)
        self.edit_backup_path = QLineEdit(_saved_custom_path)
        self.edit_backup_path.setPlaceholderText("默认：各盘根目录\\散开备份")
        cust_row.addWidget(self.edit_backup_path, 1)
        btn_browse = QPushButton("浏览...")
        btn_browse.clicked.connect(self._browse_backup_path)
        cust_row.addWidget(btn_browse)
        vB.addLayout(cust_row)
        # 警告行高常留：勾选显示橙色文字，不勾选清空文字但行还在，布局不跳
        self.lbl_backup_hint = QLabel("")
        self.lbl_backup_hint.setStyleSheet("color: #E65100;")
        self.lbl_backup_hint.setContentsMargins(24, 0, 0, 0)
        vB.addWidget(self.lbl_backup_hint)
        self.chk_backup_custom.toggled.connect(self._on_backup_custom_toggled)
        self._on_backup_custom_toggled(_saved_custom)  # 初始文字/灰显状态
        # 保留策略：不勾选 = 跟随撤回记录清理；勾选 = 超期移入回收站
        try:
            saved_days = int(s.value("backup_max_days", 0, type=int))
        except (TypeError, ValueError):
            saved_days = 0
        days_row = QHBoxLayout()
        self.chk_backup_days = QCheckBox("超过指定天数的备份移入回收站")
        self.chk_backup_days.setChecked(saved_days > 0)
        days_row.addWidget(self.chk_backup_days)
        self.spin_backup_days = QSpinBox()
        self.spin_backup_days.setRange(1, 999)
        self.spin_backup_days.setValue(saved_days if saved_days > 0 else 20)
        self.spin_backup_days.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 宽 = 3 位数字 + 左右各 6px 内边距 + 边框 + 4px 余量（999 不切字）
        self.spin_backup_days.setFixedWidth(
            self.spin_backup_days.fontMetrics().horizontalAdvance("999") + 18)
        # 与体积框同款样式：禁用态灰框灰字，勾选后才亮
        _app_ref = getattr(self, "_app", None)
        if _app_ref is not None:
            self.spin_backup_days.setStyleSheet(_app_ref._spin_style())
        self.spin_backup_days.setEnabled(saved_days > 0)
        days_row.addWidget(self.spin_backup_days)
        days_row.addWidget(QLabel("天"))
        days_row.addStretch(1)
        vB.addLayout(days_row)
        self.chk_backup_days.toggled.connect(self.spin_backup_days.setEnabled)
        lb_days_hint = QLabel(
            "不勾选：备份随撤回记录自动清理（约保留最近 20 次操作）。")
        lb_days_hint.setContentsMargins(24, 0, 0, 0)
        vB.addWidget(lb_days_hint)
        self.btn_clean_backups = QPushButton("清理失效备份（所有盘）")
        self.btn_clean_backups.clicked.connect(self._clean_backups_clicked)
        vB.addWidget(self.btn_clean_backups)
        layout.addWidget(gbB)

    # ---------- 板块二：启动与记忆设置 ----------
    def _build_memory_section(self, layout, s):
        gb2 = QGroupBox("启动与记忆设置")
        v2 = QVBoxLayout(gb2)
        v2.setContentsMargins(9, 0, 9, 9)
        v2.setSpacing(6)
        self.chk_remember_dir = QCheckBox("记忆上次的父目录路径")
        self.chk_remember_dir.setChecked(
            bool(s.value("remember_parent_dir", False, type=bool)))
        v2.addWidget(self.chk_remember_dir)
        # 后缀筛选状态记忆：与“操作配置记忆”相互独立，单独开关
        self.chk_remember_suffix = QCheckBox("记忆上次的后缀筛选状态")
        self.chk_remember_suffix.setChecked(
            bool(s.value("remember_suffix", False, type=bool)))
        v2.addWidget(self.chk_remember_suffix)
        self.chk_remember_ops = QCheckBox(
            "记忆上次的操作配置 (包括散开深度、操作方式、冲突处理)")
        self.chk_remember_ops.setChecked(
            bool(s.value("remember_ops_config", False, type=bool)))
        v2.addWidget(self.chk_remember_ops)
        layout.addWidget(gb2)

    # ---------- 板块三：行为与体验（2 个复选框） ----------
    def _build_behavior_section(self, layout, s):
        gb3 = QGroupBox("行为与体验")
        v3 = QVBoxLayout(gb3)
        v3.setContentsMargins(9, 0, 9, 9)
        v3.setSpacing(6)
        self.chk_auto_clear_log = QCheckBox("完成后自动清空日志")
        self.chk_auto_clear_log.setChecked(
            bool(s.value("auto_clear_log", False, type=bool)))
        v3.addWidget(self.chk_auto_clear_log)
        self.chk_ignore_hidden = QCheckBox(
            "散开时忽略系统隐藏文件 (如 .DS_Store, Thumbs.db)")
        self.chk_ignore_hidden.setChecked(
            bool(s.value("ignore_hidden", False, type=bool)))
        v3.addWidget(self.chk_ignore_hidden)
        layout.addWidget(gb3)

    # ---------- 板块四：系统设置（3 个进阶复选框） ----------
    def _build_system_section(self, layout, s):
        gb4 = QGroupBox("系统设置")
        v4 = QVBoxLayout(gb4)
        v4.setContentsMargins(9, 0, 9, 9)
        v4.setSpacing(6)
        self.chk_autostart = QCheckBox("开机自启")
        self.chk_autostart.setChecked(
            bool(s.value("autostart", False, type=bool)))
        v4.addWidget(self.chk_autostart)
        self.chk_close_to_tray = QCheckBox(
            "点击右上角关闭按钮时最小化到系统托盘 (不退出软件)")
        self.chk_close_to_tray.setChecked(
            bool(s.value("close_to_tray", False, type=bool)))
        v4.addWidget(self.chk_close_to_tray)
        self.chk_silent_start = QCheckBox(
            "软件启动时隐藏主界面 (静默启动，只显示托盘)")
        self.chk_silent_start.setChecked(
            bool(s.value("silent_start", False, type=bool)))
        v4.addWidget(self.chk_silent_start)
        layout.addWidget(gb4)

    # ---------- 底部按钮行（固定于滚动区外：左「重置窗口」，右「应用/保存/取消」） ----------
    def _build_button_row(self):
        btn_row = QHBoxLayout()
        # 上边距 0：内容布局底边距已是 16，总间距 16px 与主界面上边距一致
        btn_row.setContentsMargins(16, 0, 16, 12)
        btn_row.setSpacing(6)  # 与主界面操作栏按钮间距一致（原来默认 0，按钮挤在一起）
        btn_reset_win = QPushButton("重置窗口")
        btn_reset_win.setObjectName("linkButton")
        # 图标走 _emoji_icon 统一度量：🔄 是彩色 emoji（后备字体行高偏大），
        # ↺ 是普通字符，直接写进文字会把两个按钮撑得一高一低
        btn_reset_win.setIcon(_emoji_icon("🔄", 16))
        btn_reset_win.setIconSize(QSize(14, 14))
        btn_reset_win.clicked.connect(self._app._center_default)
        btn_row.addWidget(btn_reset_win)
        btn_reset_def = QPushButton("恢复默认设置")
        btn_reset_def.setObjectName("linkButton")
        btn_reset_def.clicked.connect(self._reset_defaults_clicked)
        btn_row.addWidget(btn_reset_def)
        # 恢复默认的即时反馈：内嵌小字，位于按钮与 stretch 之间，出现只压缩 stretch
        self._reset_hint = QLabel("")
        self._reset_hint.setStyleSheet("color: #0078D4; font-size: 12px;")
        self._reset_hint.setVisible(False)
        self._reset_hint_gen = 0  # 连点时只让最新一次的定时器负责隐藏
        btn_row.addWidget(self._reset_hint)
        btn_row.addStretch(1)
        btn_apply = QPushButton("应用")
        btn_apply.clicked.connect(self._apply_clicked)
        btn_row.addWidget(btn_apply)
        btn_save = QPushButton("完成")
        btn_save.setObjectName("accentButton")
        btn_save.clicked.connect(self.accept)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_save)
        btn_row.addWidget(btn_cancel)
        return btn_row

    def _apply_clicked(self):
        """「应用」：与「保存设置」同一保存路径，立即生效但不关闭窗口。"""
        if self._app is not None:
            self._app._apply_settings_from_dialog(self)

    def _reset_defaults_clicked(self):
        """「恢复默认设置」：把对话框内全部控件拨回默认值，点下即生效。
        重置后立即走一遍「应用」保存路径落盘生效，无需再点应用/完成；
        仅存主窗口的两个键（散开确认、筛选模式）直接写默认并同步主界面。"""
        def _check_radio(group, val):
            for rb in group.buttons():
                if rb.property("optval") == val:
                    rb.setChecked(True)
                    return
        _check_radio(self.theme_group, "dark")  # 默认主题 = 深色
        for cb in self.menu_checks.values():
            cb.setChecked(True)
        _check_radio(self.menu_mode_group, "move")
        _check_radio(self.menu_conflict_group, "overwrite")  # 联动启用回收站子项
        self.chk_menu_trash.setChecked(True)
        self.chk_backup_custom.setChecked(False)   # 未勾选时无信号也无妨，状态本就正确
        self.edit_backup_path.setText("")
        self.chk_backup_days.setChecked(False)
        self.spin_backup_days.setValue(20)
        for cb in (self.chk_remember_dir, self.chk_remember_suffix,
                   self.chk_remember_ops, self.chk_auto_clear_log,
                   self.chk_ignore_hidden,
                   self.chk_autostart, self.chk_close_to_tray,
                   self.chk_silent_start):
            cb.setChecked(False)
        app = self._app
        if app is not None:
            # 这两个键不在对话框里：直接写默认，并同步主界面筛选按钮文字
            app.settings.setValue("confirm_scatter", True)
            app.settings.setValue("filter_exclude", False)
            app.filter_exclude = False
            app._apply_filter_mode()
        # 点下即生效：立即走一遍「应用」保存路径，落盘并生效
        self._apply_clicked()
        self._show_reset_hint()

    def _show_reset_hint(self):
        """恢复默认后在按钮行内嵌显示「已恢复默认设置」，4 秒自动消失；
        连点时用代数标记，只让最新一次的定时器负责隐藏，避免提前熄灭。"""
        self._reset_hint.setText("已恢复默认设置")
        self._reset_hint.setVisible(True)
        self._reset_hint_gen += 1
        gen = self._reset_hint_gen
        QTimer.singleShot(4000, lambda g=gen: self._hide_reset_hint(g))

    def _hide_reset_hint(self, gen):
        if gen == self._reset_hint_gen:
            self._reset_hint.setVisible(False)

    def get_theme_mode(self):
        """返回主题三档当前选中值（system / light / dark）。"""
        for rb in self.theme_group.buttons():
            if rb.isChecked():
                return rb.property("optval") or "dark"
        return "dark"

    def get_menu_opts(self):
        return {k: cb.isChecked() for k, cb in self.menu_checks.items()}

    def get_menu_mode(self):
        """返回当前选中的右键菜单默认操作方式。"""
        for rb in self.menu_mode_group.buttons():
            if rb.isChecked():
                return rb.property("optval") or "move"
        return "move"

    def get_menu_conflict(self):
        """返回当前选中的右键菜单默认冲突处理方式。"""
        for rb in self.menu_conflict_group.buttons():
            if rb.isChecked():
                return rb.property("optval") or "rename"
        return "rename"

    def _on_menu_conflict_toggled(self, btn, checked):
        """仅选中「覆盖」时启用回收站子项（与主界面行为一致）。"""
        if btn.property("optval") == "overwrite":
            self.chk_menu_trash.setEnabled(checked)

    def _backup_label_text(self, tag, r):
        """备份行标签文字（初始化与清理后刷新共用）：数量/体积实时统计。"""
        n, size = _count_backup_root(r)
        return f"{tag}备份：{n} 个文件（{size/1048576:.1f} MB）· {r}"

    def _refresh_backup_rows(self):
        """清理备份后重数各行：文件清零的行直接移除，全没了显示「暂无备份」。"""
        for norm in list(self._backup_rows):
            info = self._backup_rows[norm]
            n, _size = _count_backup_root(info["path"])
            if n == 0:
                info["row"].setParent(None)
                info["row"].deleteLater()
                del self._backup_rows[norm]
            else:
                info["lb"].setText(
                    self._backup_label_text(info["tag"], info["path"]))
        if not self._backup_rows:
            self._backup_empty_lb.setVisible(True)

    def _clean_backups_clicked(self, root=None):
        try:
            n = self._app._clean_orphan_backups(
                [root] if root else None)
            where = "该盘" if root else "全部备份目录"
            self._refresh_backup_rows()
            QMessageBox.information(
                self, "清理失效备份",
                f"已从{where}删除 {n} 个不再被撤回栈引用的备份文件。")
        except Exception as e:
            QMessageBox.warning(self, "清理失效备份", f"清理失败：{e}")

    def _empty_backups_clicked(self, root):
        try:
            n = self._app._empty_backup_root(root)
            # 备份已进回收站：依赖它们的撤回批次整批移出栈，
            # 否则出现「没备份了还能撤回」；主日志 [备份] 提示同步更新
            dropped = self._app._invalidate_undo_backups(root)
            self._app._sync_backup_notice_log()
        except Exception as e:
            QMessageBox.warning(self, "清空备份", f"清空失败：{e}")
            return
        info = self._backup_rows.pop(os.path.normcase(root), None)
        if info is not None:
            info["row"].setParent(None)  # 从布局摘下，该行立即消失
            info["row"].deleteLater()
        if not self._backup_rows:
            self._backup_empty_lb.setVisible(True)
        if dropped:
            self._app.log_msg(
                f"[撤回] 备份已清空：对应的 {dropped} 批操作已移出撤回列表。")
        msg = f"已将 {n} 个备份文件移入回收站。"
        if dropped:
            msg += f"对应的 {dropped} 次操作已移出撤回列表，不能再撤回。"
        else:
            msg += "（需要找回旧文件可去回收站）"
        QMessageBox.information(self, "清空备份", msg)

    def _on_backup_custom_toggled(self, checked):
        """勾选自定义：警告行显示橙色跨盘提示，各盘备份现状行灰显；
        不勾选：警告文字清空但行高保留，布局不跳。"""
        self.lbl_backup_hint.setText(
            "注意：自定义目录与散开文件不同盘时，备份为复制，"
            "速度变慢且短暂占双倍空间。" if checked else "")
        for info in getattr(self, "_backup_rows", {}).values():
            for w in (info["lb"], info["btn"], info["btn2"]):
                w.setEnabled(not checked)
        if hasattr(self, "_backup_empty_lb"):
            self._backup_empty_lb.setEnabled(not checked)

    @staticmethod
    def _custom_backup_root_invalid(p):
        """自定义备份根不得为盘根或系统关键目录（F1：备份信任校验按根前缀
        放行，根若取盘根，整盘任意文件都被视为可信备份，伪造撤回记录可借
        撤回通道搬动任意文件）。"""
        if not p:
            return False
        drive, rest = os.path.splitdrive(os.path.normpath(p))
        if drive and rest in ("", "\\", "/"):
            return True
        return _is_protected_system_dir(p)

    def _browse_backup_path(self):
        d = QFileDialog.getExistingDirectory(self, "选择备份目录")
        if not d:
            return
        if self._custom_backup_root_invalid(d):
            QMessageBox.warning(
                self, "选择备份目录",
                "备份目录不能是磁盘根目录或系统关键目录，\n"
                "请选择一个普通子文件夹（如 D:\\散开备份）。")
            return
        self.edit_backup_path.setText(d)

    def _open_backup_root(self, path):
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        except Exception as e:
            QMessageBox.warning(self, "打开备份", f"打开失败：{e}")

    def save_all(self, settings):
        """把全部设置写入 QSettings（主题 + 右键菜单 + 记忆/行为开关 + 当前操作配置）。"""
        settings.setValue("theme_mode", self.get_theme_mode())
        settings.setValue("menu_opts", json.dumps(self.get_menu_opts()))
        settings.setValue("menu_mode", self.get_menu_mode())
        settings.setValue("menu_conflict", self.get_menu_conflict())
        settings.setValue("conflict_trash", self.chk_menu_trash.isChecked())
        settings.setValue("backup_max_days",
                          self.spin_backup_days.value()
                          if self.chk_backup_days.isChecked() else 0)
        # F1：手动输入的自定义备份根同样校验（浏览按钮已拦，输入框可绕过）；
        # 非法则强制不启用，回退默认「各盘\散开备份」
        _custom_on = self.chk_backup_custom.isChecked()
        _custom_path = self.edit_backup_path.text().strip()
        if _custom_on and self._custom_backup_root_invalid(_custom_path):
            QMessageBox.warning(
                self, "备份目录",
                "自定义备份目录不能是磁盘根目录或系统关键目录，已改回默认。")
            _custom_on = False
            self.chk_backup_custom.setChecked(False)
        settings.setValue("backup_custom_enabled", _custom_on)
        settings.setValue("backup_custom_path", _custom_path)
        settings.setValue("remember_parent_dir", self.chk_remember_dir.isChecked())
        if not self.chk_remember_dir.isChecked():
            # L4：关闭记忆立即清掉残留路径（旧行为等关窗才清，
            # 「保存设置后杀进程」会把旧路径留在注册表）
            settings.remove("last_parent_dir")
        settings.setValue("remember_suffix", self.chk_remember_suffix.isChecked())
        settings.setValue("remember_ops_config", self.chk_remember_ops.isChecked())
        settings.setValue("auto_clear_log", self.chk_auto_clear_log.isChecked())
        settings.setValue("ignore_hidden", self.chk_ignore_hidden.isChecked())
        app = self._app
        # 操作配置记忆（散开模式 / 操作方式 / 冲突处理）：勾选写入，否则清除
        if self.chk_remember_ops.isChecked() and app is not None:
            settings.setValue("last_level", app.level)
            settings.setValue("last_mode", app.mode)
            settings.setValue("last_conflict", app.conflict)
        else:
            for k in ("last_level", "last_mode", "last_conflict"):
                settings.remove(k)
        # 后缀筛选状态记忆（与操作配置记忆相互独立）：勾选写入，否则清除。
        # 「忽略体积」不在此列——GUI 变更即落盘，右键静默共用同一键
        if self.chk_remember_suffix.isChecked() and app is not None:
            states = [cb.text() for cb in app.suffix_checks if cb.isChecked()]
            settings.setValue("suffix_states", ",".join(states))
            settings.setValue("custom_suffix", app.edit_custom_suffix.text())
        else:
            for k in ("suffix_states", "custom_suffix"):
                settings.remove(k)
        # 系统设置：开机自启 / 关闭最小化到托盘 / 静默启动
        settings.setValue("autostart", self.chk_autostart.isChecked())
        settings.setValue("close_to_tray", self.chk_close_to_tray.isChecked())
        settings.setValue("silent_start", self.chk_silent_start.isChecked())
        # 开机自启：写入/删除 HKCU\...\Run 注册表项（HKCU 无需管理员权限）
        _set_autostart(self.chk_autostart.isChecked())
