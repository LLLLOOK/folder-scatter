import ctypes


# ---------- Windows 任务栏进度条（Qt6 已移除 QtWinExtras，改用 ctypes 调 ITaskbarList3） ----------
class TaskbarProgress:
    """在 Windows 任务栏窗口按钮上显示进度。Qt6 没有原生 API，这里直接调
    ITaskbarList3 COM 接口。任何调用失败都静默降级（不抛异常、不影响主流程）；
    非 Windows 环境全部 no-op。"""
    TBPF_NOPROGRESS = 0
    TBPF_INDETERMINATE = 1
    TBPF_NORMAL = 2

    _iface = None
    _vtable = None
    _failed = False

    @classmethod
    def _get_iface(cls):
        if cls._iface is not None or cls._failed:
            return cls._iface
        try:
            import ctypes
            import uuid

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                            ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_byte * 8)]

                def __init__(self, s):
                    super().__init__()
                    u = uuid.UUID(s)
                    self.Data1 = u.time_low
                    self.Data2 = u.time_mid
                    self.Data3 = u.time_hi_version
                    for i in range(8):
                        self.Data4[i] = u.bytes[8 + i]

            IID = GUID("{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}")
            CLSID = GUID("{56FDF344-FD6D-11d0-958A-006097C9A090}")
            VOIDP = ctypes.c_void_p
            HRESULT = ctypes.c_long
            HWND = ctypes.c_void_p
            ULL = ctypes.c_ulonglong

            class Vtbl(ctypes.Structure):
                _fields_ = [
                    ("QueryInterface", VOIDP), ("AddRef", VOIDP), ("Release", VOIDP),
                    ("HrInit", ctypes.WINFUNCTYPE(HRESULT, VOIDP)),
                    ("AddTab", VOIDP), ("DeleteTab", VOIDP), ("ActivateTab", VOIDP),
                    ("SetActiveAlt", VOIDP), ("MarkFullscreenWindow", VOIDP),
                    ("SetProgressValue", ctypes.WINFUNCTYPE(HRESULT, VOIDP, HWND, ULL, ULL)),
                    ("SetProgressState", ctypes.WINFUNCTYPE(HRESULT, VOIDP, HWND, ctypes.c_int)),
                ]

            class ITL3(ctypes.Structure):
                _fields_ = [("lpVtbl", ctypes.POINTER(Vtbl))]

            pp = VOIDP()
            # 确保 COM 已初始化：Qt GUI 线程通常已初始化；未初始化则此处补一次。
            # 已初始化时 CoInitializeEx 返回 S_FALSE 无害；其它错误（如模式冲突）忽略。
            try:
                ctypes.windll.ole32.CoInitializeEx(None, 2)
            except Exception:
                pass
            hr = ctypes.windll.ole32.CoCreateInstance(
                ctypes.byref(CLSID), None, 1, ctypes.byref(IID), ctypes.byref(pp))
            if hr != 0 or not pp:
                cls._failed = True
                return None
            iface = ctypes.cast(pp, ctypes.POINTER(ITL3))
            vt = iface.contents.lpVtbl.contents
            if vt.HrInit(iface) != 0:
                cls._failed = True
                return None
            cls._iface = iface
            cls._vtable = vt
            return iface
        except Exception:
            cls._failed = True
            return None

    @classmethod
    def _state(cls, hwnd, state):
        iface = cls._get_iface()
        if iface is None:
            return
        try:
            cls._vtable.SetProgressState(iface, ctypes.c_void_p(hwnd), state)
        except Exception:
            pass

    @classmethod
    def set_indeterminate(cls, hwnd):
        cls._state(hwnd, cls.TBPF_INDETERMINATE)

    @classmethod
    def set_normal(cls, hwnd, current, total):
        iface = cls._get_iface()
        if iface is None:
            return
        try:
            vt = cls._vtable
            vt.SetProgressValue(iface, ctypes.c_void_p(hwnd),
                                ctypes.c_ulonglong(current), ctypes.c_ulonglong(total))
            vt.SetProgressState(iface, ctypes.c_void_p(hwnd), cls.TBPF_NORMAL)
        except Exception:
            pass

    @classmethod
    def clear(cls, hwnd):
        cls._state(hwnd, cls.TBPF_NOPROGRESS)


def taskbar_available():
    """探测 Windows 任务栏进度条是否可用。

    等价于清单里写的「try: from PyQt6.QtWinExtras import QWinTaskbarButton,
    QWinTaskbarProgress; except: = None」。但 PyQt6 已彻底移除 QtWinExtras 模块，
    所以这里改用 ctypes 探测 ITaskbarList3：非 Windows 或 COM 初始化失败时，
    静默返回 False（即清单里的「None」），绝不抛异常。
    返回 False 时，程序只使用主界面进度条 self.progress，不触碰任务栏。"""
    try:
        return TaskbarProgress._get_iface() is not None
    except Exception:
        return False
