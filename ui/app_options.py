"""App 拆分子模块：选项回调 / 记忆设置 / 后缀解析 / 设置对话框 / 目录保护提示
（自 ui/main_window.py 逐字搬移，行为冻结）。

OptionsMixin 无任何 __init__，全部方法仍写 self（经 App 多继承解析），调用面零改动
（settings_dialog 的 app._apply_settings_from_dialog / app._apply_filter_mode()、
main.py 的 window.preload_folder(...) 均经继承解析）。
"""

import os
import re
import json

from PyQt6.QtWidgets import (
    QDialog,
    QMessageBox,
)

from win32.menu import MENU_REG_VERSION, apply_menu_registry
from core.guards import _is_protected_system_dir
from ui.settings_dialog import SettingsDialog
from ui.style import _translate_menu


class OptionsMixin:
    # ---------- 右键菜单设置 ----------
    def preload_folder(self, path):
        """右键菜单以普通路径参数启动 GUI 时，预载该文件夹并刷新目录树。"""
        path = os.path.normpath(path)
        if not os.path.isdir(path):
            return
        if self._is_root_dir(path) or _is_protected_system_dir(path):
            return  # 受保护目录不预载（refresh 会弹模态框，启动期不宜）
        self.entry_path.setText(path)
        self.refresh()

    def open_settings(self):
        """打开「设置」窗口；保存时写入所有偏好，右键菜单变化时重新注册。"""
        dlg = SettingsDialog(self)
        dlg._last_applied_menu = self.settings.value("menu_opts", "")
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._apply_settings_from_dialog(dlg, show_info=True)

    def _apply_settings_from_dialog(self, dlg, show_info=False):
        """设置保存的统一路径（「保存设置」与「应用」共用）：
        写入全部偏好 → 右键菜单勾选变化才重新注册 → 主题（含跟随系统）重新解析并应用。"""
        dlg.save_all(self.settings)
        # 设置里的回收站子项与主界面「覆盖-回收站」是同一开关：保存后同步主界面勾选态
        self.chk_trash.setChecked(
            bool(self.settings.value("conflict_trash", True, type=bool)))
        # 右键菜单勾选变化，或菜单注册版本号升级（文案/命令改版）时才重写注册表
        new_menu = self.settings.value("menu_opts", "")
        reg_ver = str(self.settings.value("menu_reg_version", ""))
        if new_menu != getattr(dlg, "_last_applied_menu", None) or \
                reg_ver != MENU_REG_VERSION:
            try:
                opts = json.loads(new_menu)
                # winreg 直写 HKCU（无需管理员、不弹 UAC、同步完成）
                apply_menu_registry(opts)
                self.settings.setValue("menu_reg_version", MENU_REG_VERSION)
            except Exception as e:
                QMessageBox.warning(self, "错误", "右键菜单注册失败：" + str(e))
        dlg._last_applied_menu = new_menu
        # 主题可能在设置中被改动（含「跟随系统」）：重新解析并立即应用
        self.theme_mode = str(self.settings.value("theme_mode", "dark"))
        self.is_dark = self._resolve_is_dark(self.theme_mode)
        self.apply_theme()
        # 不弹窗打扰：统一走状态栏文字反馈
        self.status_label.setText("设置已保存" if show_info else "设置已应用")

    def open_parent_dir(self):
        """在资源管理器中打开当前输入的父目录（无效路径仅状态栏提示，不弹窗打扰）。"""
        p = self.entry_path.text().strip()
        if not p or not os.path.isdir(p):
            self.status_label.setText("没有可打开的有效目录")
            return
        try:
            os.startfile(p)
        except OSError as e:
            self.status_label.setText(f"打开目录失败：{e}")

    # ---------- 偏好 / 记忆 应用 ----------
    @staticmethod
    def _set_group_by_val(group, val):
        """按 optval 把对应单选按钮设为选中。"""
        for rb in group.buttons():
            if rb.property("optval") == val:
                rb.setChecked(True)
                return

    def _restore_memory_settings(self):
        """启动时恢复「启动与记忆设置」。

        - 记住父目录路径：恢复上次路径并刷新目录树。
        - 记住上次的操作配置：勾选则从 QSettings 恢复 散开模式/操作方式/冲突处理/后缀；
          未勾选则恢复代码写死的初始状态（仅当前层散开 / 移动(剪切) / 覆盖 / 不勾选任何后缀）。
        - 「忽略体积」（ignore_checked/ignore_size）不跟随后缀记忆：始终按保存值恢复，
          GUI 变更即落盘，右键静默共用同一键（语义同 conflict_trash）。
        """
        s = self.settings
        if bool(s.value("remember_parent_dir", False, type=bool)):
            p = s.value("last_parent_dir", "")
            if p and os.path.isdir(p):
                self.entry_path.setText(p)
                self.refresh()  # 异步重建目录树

        if bool(s.value("remember_ops_config", False, type=bool)):
            self._set_group_by_val(self.level_group,
                                   s.value("last_level", "one"))
            self._set_group_by_val(self.mode_group,
                                   s.value("last_mode", "move"))
            self._set_group_by_val(self.conflict_group,
                                   s.value("last_conflict", "overwrite"))
        else:
            # 写死初始状态（散开模式 / 操作方式 / 冲突处理）
            self._set_group_by_val(self.level_group, "one")
            self._set_group_by_val(self.mode_group, "move")
            self._set_group_by_val(self.conflict_group, "overwrite")
        if bool(s.value("remember_suffix", False, type=bool)):
            states = [x for x in (s.value("suffix_states", "") or "").split(",") if x]
            for cb in self.suffix_checks:
                cb.setChecked(cb.text() in states)
            self.edit_custom_suffix.setText(s.value("custom_suffix", "") or "")
            self._validate_custom_suffix()  # 载入记忆的后缀后立即同步边框状态
        else:
            # 写死初始状态（后缀筛选相关）
            for cb in self.suffix_checks:
                cb.setChecked(False)
            self.edit_custom_suffix.clear()
        # 「忽略体积」不再跟随「记忆后缀筛选」：它是 GUI/右键共用的行为开关，
        # 始终按保存值恢复（语义同 conflict_trash），保证两边行为一致
        self.chk_ignore.setChecked(
            bool(s.value("ignore_checked", False, type=bool)))
        try:
            self.spin_max.setValue(int(s.value("ignore_size", 500)))
        except (TypeError, ValueError):
            self.spin_max.setValue(500)

    def _persist_memory_settings(self):
        """保存/清除「启动与记忆设置」对应的持久化数据（在保存设置或关闭软件时调用）。"""
        s = self.settings
        # 父目录路径
        if bool(s.value("remember_parent_dir", False, type=bool)):
            s.setValue("last_parent_dir", self.entry_path.text().strip())
        else:
            s.remove("last_parent_dir")
        # 操作配置记忆（散开模式 / 操作方式 / 冲突处理）：勾选写入，否则清除
        if bool(s.value("remember_ops_config", False, type=bool)):
            s.setValue("last_level", self.level)
            s.setValue("last_mode", self.mode)
            s.setValue("last_conflict", self.conflict)
        else:
            for k in ("last_level", "last_mode", "last_conflict"):
                s.remove(k)
        # 后缀筛选状态记忆（与操作配置记忆相互独立）：勾选写入，否则清除。
        # 「忽略体积」不在此列——GUI 变更即落盘（_on_ignore_*），右键静默共用
        if bool(s.value("remember_suffix", False, type=bool)):
            states = [cb.text() for cb in self.suffix_checks if cb.isChecked()]
            s.setValue("suffix_states", ",".join(states))
            s.setValue("custom_suffix", self.edit_custom_suffix.text())
        else:
            for k in ("suffix_states", "custom_suffix"):
                s.remove(k)

    def _save_parent_dir(self):
        """当前父目录变更时调用：若开启记忆则保存，否则清除。"""
        s = self.settings
        if bool(s.value("remember_parent_dir", False, type=bool)):
            s.setValue("last_parent_dir", self.entry_path.text().strip())
        else:
            s.remove("last_parent_dir")

    def _show_root_warning(self):
        """在目录树工具栏显示红色磁盘根目录预警提示。"""
        self.root_warning_label.setText(
            "⚠️ 提示：磁盘根目录，请选择具体子文件夹。")
        self.root_warning_label.setVisible(True)

    def _show_system_warning(self):
        """在目录树工具栏显示红色系统关键目录预警提示。"""
        self.root_warning_label.setText(
            "⚠️ 禁止：系统关键目录，散开会导致 Windows 崩溃。")
        self.root_warning_label.setVisible(True)

    @staticmethod
    def _is_root_dir(path):
        """判断 path 是否为磁盘根目录（如 E:、C: 等磁盘根目录）。"""
        drive, rest = os.path.splitdrive(os.path.normpath(path))
        return bool(drive) and rest in ("", "\\", "/")

    # ---------- 单选组回调 ----------
    def _set_level(self, checked, val):
        if checked:
            # 散开深度不参与目录树勾选：只记录深度，散开时由引擎按深度在后台展开勾选的文件夹
            self.level = val

    def _set_mode(self, checked, val):
        if checked:
            self.mode = val

    def _set_conflict(self, checked, val):
        if checked:
            self.conflict = val
        # 仅选中「覆盖」时亮起「覆盖-回收站可找回」复选框；切换到跳过/重命名则灰显禁用
        self.chk_trash.setEnabled(checked and val == "overwrite")
        self._update_chk_trash_color()

    def _on_chk_trash_toggled(self, checked):
        """回收站复选框状态变化时：持久化 + 同步文字颜色。"""
        self.settings.setValue("conflict_trash", checked)
        self._update_chk_trash_color()

    def _update_chk_trash_color(self):
        """根据复选框的启用/勾选状态同步文字颜色。
        未勾选或被禁用时显示与『忽略大于此体积(MB)』右侧 500 一致的灰色。"""
        if self.chk_trash.isChecked() and self.chk_trash.isEnabled():
            self.chk_trash.setStyleSheet("")  # 继承主题原色
        else:
            self.chk_trash.setStyleSheet("QCheckBox { color: #808080; }")

    # ---------- 后缀解析 ----------
    def _show_suffix_context_menu(self, pos):
        """自定义后缀输入框的右键菜单：保留 Qt 标准样式，仅将英文文字替换为中文。"""
        menu = self.edit_custom_suffix.createStandardContextMenu()
        _translate_menu(menu)
        menu.exec(self.edit_custom_suffix.mapToGlobal(pos))

    def _toggle_filter_mode(self):
        """切换后缀筛选模式：只散开（白名单）↔ 只排除（黑名单），并持久化。"""
        self.filter_exclude = not self.filter_exclude
        self.settings.setValue("filter_exclude", self.filter_exclude)
        self._apply_filter_mode()

    def _apply_filter_mode(self):
        """按当前 filter_exclude 设置按钮文字与提示（启动回读与切换共用）。"""
        if self.filter_exclude:
            self.btn_filter_mode.setText("只排除")
            self.btn_filter_mode.setToolTip(
                "当前：只排除勾选的后缀（黑名单），散开其余所有文件。\n"
                "点击切换为「只散开」：只散开勾选后缀的文件。")
        else:
            self.btn_filter_mode.setText("只散开")
            self.btn_filter_mode.setToolTip(
                "当前：只散开勾选的后缀（白名单）。\n"
                "点击切换为「只排除」：散开除勾选后缀外的所有文件。")

    def _parse_suffixes(self):
        """收集 4 个复选框 + 自定义输入框的后缀；空返回 None（代表全部文件）。
        自定义框同时支持中英文逗号和空格混合分割（如 'psd png c4d' / 'psd, png, c4d' / 'psd，png'）。"""
        res = set()
        for cb in self.suffix_checks:
            if cb.isChecked():
                res.add(cb.text().strip().lstrip(".").lower())
        raw = self.edit_custom_suffix.text().strip()
        if raw:
            # 中英文逗号或空白（含连续空格）均作为分隔符；分割规则必须与
            # _validate_custom_suffix 一致，否则中文逗号输入会「校验通过但
            # 解析成整坨后缀」——白名单 0 命中、黑名单全放行且不标红
            for part in re.split(r"[,，\s]+", raw):
                part = part.strip().lstrip(".").lower()
                if part:
                    res.add(part)
        return res if res else None

    def _validate_custom_suffix(self):
        """自定义后缀输入框实时校验：按 re.split(逗号/空格) 分割并去前导点后，
        每个片段都必须是纯小写字母/数字（分割正则与 _parse_suffixes 保持一致），
        否则红框并记入 _custom_suffix_valid（开始散开/模拟时据此拦截）。
        纯空格/纯逗号等「只有分隔符、无任何有效字符」视作空框（不过滤），不标红。"""
        raw = self.edit_custom_suffix.text()
        # 去掉所有空格与中英文逗号后若为空，等同未输入（不过滤），不标红
        if not re.sub(r"[\s,，]+", "", raw):
            self._custom_suffix_valid = True
            self.edit_custom_suffix.setStyleSheet("")  # 空框：正常边框
            return
        invalid = []
        for part in re.split(r"[,，\s]+", raw):
            p = part.strip().lstrip(".").lower()
            if p and not re.fullmatch(r"[a-z0-9]+", p):
                invalid.append(part)
        self._custom_suffix_valid = not invalid
        if invalid:
            self.edit_custom_suffix.setStyleSheet("border: 1px solid #EF5350;")
        else:
            self.edit_custom_suffix.setStyleSheet("")

    # ---------- 忽略体积门控 ----------
    def _on_ignore_toggled(self, checked):
        self.spin_max.setEnabled(checked)
        # 「忽略体积」是 GUI 与右键静默共用的行为开关（右键只读 QSettings）：
        # 变更即落盘，不再跟随「记忆后缀筛选」——否则关掉记忆后键被清除，
        # 右键静默永远不忽略大文件且无任何提示（语义同 conflict_trash）
        self.settings.setValue("ignore_checked", checked)

    def _on_ignore_size_changed(self, val):
        """体积上限变更即持久化：右键静默模式读同一键，须与界面实时一致。"""
        self.settings.setValue("ignore_size", val)
