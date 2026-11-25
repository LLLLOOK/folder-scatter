from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QWidget,
)

from PyQt6.QtCore import (
    QPropertyAnimation,
    QSize,
    QTimer,
    Qt,
)

from PyQt6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QPixmap,
)
from ui.icons import _emoji_icon, _load_app_icon


# ---------- 自定义 Toast 悬浮通知（替代托盘气泡，不依赖 Windows 通知中心） ----------
_LIVE_TOASTS = []  # 存活 Toast 注册表：静默进程退出前泵循环等它们淡出


class ToastNotify(QWidget):
    """右下角无边框半透明 Toast，替代 QSystemTrayIcon.showMessage。
    不依赖 Windows 通知中心，创建即显示；2.5s 后自动淡出关闭。
    用于右键静默散开的开始/完成反馈，以及主程序收到 done 时的完成提示。"""

    def __init__(self, text="后台散开任务已启动。", parent=None):
        super().__init__(parent)
        _LIVE_TOASTS.append(self)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # 左侧软件图标（20px）：取 appicon，缺失退回📂表情图标，静默进程同样可用
        icon = _load_app_icon()
        pm = icon.pixmap(QSize(20, 20)) if not icon.isNull() else QPixmap()
        if pm.isNull():
            pm = _emoji_icon("📂", 20).pixmap(QSize(20, 20))
        icon_lb = QLabel(self)
        icon_lb.setPixmap(pm)
        icon_lb.setStyleSheet("background: transparent;")
        # 右侧文字：过长省略号截断，框宽不失控
        text_lb = QLabel(self)
        f = text_lb.font()
        f.setPixelSize(13)
        text_lb.setFont(f)
        text_lb.setStyleSheet("QLabel { color: white; background: transparent; }")
        fm = text_lb.fontMetrics()
        max_text_w = 340
        if fm.horizontalAdvance(text) > max_text_w:
            text = fm.elidedText(text, Qt.TextElideMode.ElideRight, max_text_w)
        text_lb.setText(text)
        # 尺寸贴着内容：左14 + 图标20 + 间距8 + 文字宽 + 右16，高40，紧凑不空荡
        self.setFixedSize(14 + 20 + 8 + fm.horizontalAdvance(text) + 16, 40)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 16, 10)
        layout.setSpacing(8)
        layout.addWidget(icon_lb, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(text_lb, 0, Qt.AlignmentFlag.AlignVCenter)
        # 蓝底（稍透明）圆角用 paintEvent 自绘：QSS 对无 Q_OBJECT 的自定义
        # 组件不可靠（类名选择器/无选择器写法都曾被盖成深色），自绘任何环境都稳
        self._fade = QPropertyAnimation(self, b"windowOpacity")
        self._fade.setDuration(400)
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self.close)
        QTimer.singleShot(2500, self._begin_fade)

    def paintEvent(self, e):
        from PyQt6.QtGui import QPainter, QBrush, QColor
        from PyQt6.QtCore import QRectF
        p = QPainter(self)
        # 先把圆角外清成全透明（Source 模式连 alpha 一起替换），
        # 否则残留半透明像素会在桌面上形成方形光晕
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), QColor(0, 0, 0, 0))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(0, 120, 212, 200)))  # 强调蓝、半透明
        p.drawRoundedRect(QRectF(self.rect()), 12, 12)  # 更圆的角
        p.end()

    def _begin_fade(self):
        self._fade.start()

    def closeEvent(self, e):
        if self in _LIVE_TOASTS:
            _LIVE_TOASTS.remove(self)
        super().closeEvent(e)

    def showEvent(self, e):
        # 定位到右下角（任务栏上方）；用光标所在屏幕，适配多屏与任意缩放
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            margin = 12
            x = geo.x() + geo.width() - self.width() - margin
            y = geo.y() + geo.height() - self.height() - margin
            self.move(x, y)
        super().showEvent(e)
