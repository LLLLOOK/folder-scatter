import os

from PyQt6.QtCore import QRect, QSize, Qt

from infra.paths import IS_FROZEN, RESOURCE_DIR, _undo_base

from PyQt6.QtGui import QIcon, QPixmap


def _emoji_icon(ch, size=18):
    """把彩色表情画成 QIcon（Segoe UI Emoji，带缓存）。
    表情直接嵌按钮文本时字形度量会弄歪左右留白，改图标后按钮与普通按钮同款居中。"""
    from PyQt6.QtGui import QPixmap, QPainter, QFont
    from PyQt6.QtCore import QRect
    cache = getattr(_emoji_icon, "_cache", None)
    if cache is None:
        cache = _emoji_icon._cache = {}
    key = (ch, size)
    if key in cache:
        return cache[key]
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    f = QFont("Segoe UI Emoji")
    f.setPixelSize(size - 4)  # 画布留边距，防表情下缘/外圈被切
    p.setFont(f)
    # 水平左对齐：字形贴画布左缘，图标-文字间距只剩样式间距，更紧凑；
    # 垂直居中且 pixelSize 留边距，防下缘被切
    p.drawText(QRect(0, 0, size, size),
               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, ch)
    p.end()
    ic = QIcon(pm)
    cache[key] = ic
    return ic


def _load_app_icon():
    """从随程序带走的多尺寸 PNG（16/32/64/128/256/512）组装窗口图标。

    兼容：开发态（PNG 在项目根）、PyInstaller --onefile（_MEIPASS）、
    PyInstaller --onedir / Nuitka（随 exe 分发的资源目录）。
    """
    icon = QIcon()
    candidates = []
    if IS_FROZEN:
        candidates.append(RESOURCE_DIR)
        candidates.append(_undo_base)
    candidates.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for size in (16, 32, 64, 128, 256, 512):
        for base in candidates:
            p = os.path.join(base, f"appicon_{size}.png")
            if os.path.exists(p):
                icon.addFile(p, QSize(size, size))
                break
    return icon
