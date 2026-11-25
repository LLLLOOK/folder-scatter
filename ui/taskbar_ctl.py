"""任务栏进度条控制（右键散开忙碌态 + 本地散开百分比）。

行为冻结自 ui/main_window.py App 的 _taskbar_* 方法族；与 App 的交互
收窄为构造注入的回调（hwnd / 主界面进度条当前值，均懒解析——构造时
进度条控件尚未创建）。App 保留同名薄壳与直通属性，调用点零改动。
"""

from PyQt6.QtCore import QTimer

from win32.taskbar import TaskbarProgress, taskbar_available


class TaskbarController:
    """主窗口任务栏按钮进度条：右键散开显示“忙碌”态，本地散开显示百分比。"""

    def __init__(self, parent, hwnd, progress_value):
        self._hwnd = hwnd  # 懒解析 int(app.winId())
        self._progress_value = progress_value  # 懒解析 app.progress.value()
        # 右键散开时在主窗口任务栏按钮显示“忙碌”进度条（仅软件开着时有效）
        self._remote_active = False
        self._timer = QTimer(parent)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.clear)
        # 任务栏进度条可用性探测（跨平台 / 初始化失败保护）：
        # 不可用时 supported=False，所有任务栏相关代码静默跳过，仅用主界面 progress。
        self.supported = taskbar_available()
        self._local_total = 0  # 本地散开任务栏进度的文件总数（扫描完才有）

    # ---------- 任务栏进度条（右键散开时） ----------
    def start(self):
        """右键散开进行中：主窗口任务栏按钮显示绿色“忙碌”进度条。"""
        if not self.supported:
            return
        try:
            hwnd = self._hwnd()
        except Exception:
            return
        TaskbarProgress.set_indeterminate(hwnd)
        self._remote_active = True
        self._timer.start(30000)  # 看门狗：异常未发 done 时 30s 自动清除

    def done(self):
        """右键散开完成：进度条先置 100%，1.5s 后清除。"""
        if not self.supported:
            return
        try:
            hwnd = self._hwnd()
        except Exception:
            return
        TaskbarProgress.set_normal(hwnd, 1, 1)
        self._remote_active = False
        self._timer.start(1500)

    def clear(self):
        if not self.supported:
            return
        try:
            hwnd = self._hwnd()
        except Exception:
            return
        TaskbarProgress.clear(hwnd)
        self._remote_active = False

    # ---------- 任务栏进度条（本地「开始散开」时） ----------
    def local_max(self, total):
        """子线程扫描出文件总数：任务栏从忙碌态转绿色百分比进度。"""
        self._local_total = int(total or 0)
        if not self.supported or self._local_total <= 0:
            return
        try:
            TaskbarProgress.set_normal(
                self._hwnd(), self._progress_value(), self._local_total)
        except Exception:
            pass

    def local_prog(self, cur):
        """本地散开进度同步任务栏；总数还没出来时保持忙碌态不重复设置。"""
        total = getattr(self, "_local_total", 0)
        if not self.supported or total <= 0:
            return
        try:
            TaskbarProgress.set_normal(self._hwnd(), int(cur), total)
        except Exception:
            pass
