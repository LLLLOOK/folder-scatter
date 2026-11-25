"""App 拆分子模块：主题解析/切换与局部样式（自 ui/main_window.py 逐字搬移，行为冻结）。

ThemeMixin 无任何 __init__，全部方法仍写 self（经 App 多继承解析），调用面零改动
（main.py 的 App._resolve_is_dark、settings_dialog 的 app._spin_style() 均经继承解析）。
"""

try:
    import winreg
except ImportError:  # 非 Windows 平台（如交叉调试）保持可导入
    winreg = None

from PyQt6.QtWidgets import (
    QApplication,
)

from PyQt6.QtCore import (
    QSize,
)

from ui.icons import _emoji_icon
from ui.style import DARK_FG, LIGHT_FG, _build_stylesheet


class ThemeMixin:
    # ---------- 主题 ----------
    @staticmethod
    def _resolve_is_dark(theme_mode):
        """按主题模式解析实际亮暗：dark 恒暗；system 读 Windows 个性化注册表（AppsUseLightTheme：0=系统暗色）；其余（含读取失败）按亮色。"""
        if theme_mode == "dark":
            return True
        if theme_mode == "system" and winreg is not None:
            try:
                with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                    v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
                    return int(v) == 0
            except Exception:
                return False
        return False

    def apply_theme(self):
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(_build_stylesheet(self.is_dark))
        # 日志颜色族 + 日志控件样式 + 已有日志按新主题重新着色（贴底跟随策略逐字保留）
        self.log_panel.apply_theme(self.is_dark)
        if hasattr(self, "skip_label"):
            self.skip_label.setStyleSheet(f"color: {self._log_warn};")
            self._refresh_skip_label()  # 用新主题颜色重建警示条 HTML
        if hasattr(self, "root_warning_label"):
            self.root_warning_label.setStyleSheet(f"color: {self._log_error};")
        if hasattr(self, "spin_max"):
            self.spin_max.setStyleSheet(self._spin_style())  # 边框随主题刷新
        # 按钮文案 = 点击后要切换到的目标主题（动作导向）；tooltip 说明当前状态
        cur = "暗色" if self.is_dark else "亮色"
        tgt = "亮色" if self.is_dark else "暗色"
        note = "（跟随系统）" if getattr(self, "theme_mode", "") == "system" else ""
        # 表情走图标（QIcon），文本纯字：两态自然同宽，左右留白与普通按钮一致，
        # 也避免表情嵌文本时字形度量弄歪居中
        self.theme_btn.setIcon(_emoji_icon("☀️" if self.is_dark else "🌙", 16))
        self.theme_btn.setIconSize(QSize(14, 14))
        self.theme_btn.setText(tgt)
        self.theme_btn.setToolTip(f"当前：{cur}{note}；点击切换为{tgt}")
        # 暗色/亮色使用同一套系统图标（不做背景透明化处理，两主题观感一致），仅统一图标尺寸
        if hasattr(self, "tree"):
            self.tree.setIconSize(QSize(16, 16))
            self.tree.setStyleSheet(self._tree_style())  # 选中项文字色随主题刷新

    def toggle_theme(self):
        self.is_dark = not self.is_dark
        # 手动切换即脱离「跟随系统」，固化本次选择
        self.theme_mode = "dark" if self.is_dark else "light"
        self.apply_theme()
        self.settings.setValue("theme_mode", self.theme_mode)

    # ---------- 忽略体积门控 ----------
    def _spin_style(self):
        """数字框样式与分组框（冲突处理等）保持一致：用同款主题边框色（暗色 #404040 / 亮色 #cccccc），
        border-radius: 4px，禁用态也保留同款边框，整体融入背景不再抢眼。"""
        border = "#404040" if self.is_dark else "#cccccc"  # 与 QGroupBox 的 {border} 完全一致
        return (
            "QSpinBox {"
            f" border: 1px solid {border}; border-radius: 4px; padding: 2px 6px;"
            " text-align: center; outline: none; background: transparent;"
            "}"
            "QSpinBox:disabled {"
            f" border: 1px solid {border}; color: #999999; background: transparent;"
            "}"
            "QSpinBox::up-button, QSpinBox::down-button { width: 0px; border: none; background: transparent; }"
        )

    def _tree_style(self):
        """目录树样式（随主题）：选中/悬停用半透明灰底 + 1px 描边，行高收窄。
        选中项显式指定文字色为主题前景色，避免目录树失焦（如点了工具栏按钮）后
        Qt 按「非活动」态渲染导致选中文件夹名字变黑看不清。"""
        fg = DARK_FG if self.is_dark else LIGHT_FG
        return f"""
            QTreeWidget {{ outline: none; }}
            QTreeWidget::item {{ padding: 2px 4px; outline: none;
                                border: 1px solid transparent; border-radius: 3px; }}
            QTreeWidget::item:selected,
            QTreeWidget::item:hover:selected {{
                background-color: rgba(128, 128, 128, 0.30);
                border: 1px solid rgba(160, 160, 160, 0.55);
                color: {fg};
            }}
            QTreeWidget::item:hover {{
                background-color: rgba(128, 128, 128, 0.15);
            }}
            QTreeWidget::branch {{ outline: none; }}
        """
