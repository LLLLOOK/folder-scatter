import contextlib
import ctypes


# ---------- 跨进程散开互斥锁（Windows 命名互斥体） ----------
# 防止 GUI worker 与右键独立进程（或多个右键进程）同时移动同一批文件，
# 引发 WinError 183「文件已存在」、撤回记录错乱。
# 用内核命名互斥体而非锁文件：持锁线程/进程死亡（含崩溃、强杀）时系统自动
# 释放所有权——没有僵尸锁、没有 PID 存活检测、也没有文件锁「删锁-建锁」之间
# 的 TOCTOU 窗口（迟到的删除可能误删他人新锁）。
# 注意：互斥体所有权属于线程，必须在同一线程内获取与释放。
_MUTEX_NAME = r"Local\FolderScatterMutex"


_mutex_handle = None


class _ScatterBusyError(RuntimeError):
    """另一个散开任务持有互斥锁时抛出。GUI 路径已用 emit_log 打过原因，
    静默路径由 __main__ 捕获后弹窗告知（否则右键锁冲突用户零感知）。"""


def _acquire_scatter_lock(wait_ms=0):
    """获取散开互斥锁，返回 (ok, reason)。
    wait_ms=0 非阻塞（GUI 用：立即失败提示用户）；
    wait_ms>0 最多等待 wait_ms 毫秒（右键静默进程用：Explorer 多选会为每个
    文件夹各起一个进程，排队等锁即可顺序散开，而不是互相冲突报错）。
    WAIT_OBJECT_0    = 获得所有权；
    WAIT_ABANDONED   = 原持有线程已死，所有权归我（内核自动清僵尸）；
    WAIT_TIMEOUT     = 等待超时仍被他人持有，拒绝。"""
    global _mutex_handle
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        h = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        if not h:
            return False, "无法创建散开互斥锁（CreateMutex 失败）。"
        wr = kernel32.WaitForSingleObject(h, wait_ms)
        if wr in (0x0, 0x80):  # WAIT_OBJECT_0 / WAIT_ABANDONED
            _mutex_handle = h
            return True, ""
        kernel32.CloseHandle(h)  # WAIT_TIMEOUT：等待超时仍被他人持有
        return False, "另一个散开任务正在进行，请稍后重试。"
    except Exception as e:
        return False, f"散开互斥锁异常：{e}"


def _release_scatter_lock():
    """释放互斥锁（必须由持有线程调用；进程退出时内核亦会兜底回收）。"""
    global _mutex_handle
    if _mutex_handle:
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.ReleaseMutex(ctypes.c_void_p(_mutex_handle))
            kernel32.CloseHandle(ctypes.c_void_p(_mutex_handle))
        except Exception:
            pass
        _mutex_handle = None


@contextlib.contextmanager
def _history_lock(wait_ms=3000):
    """撤回历史写锁（A2 移植）：与散开/撤回共用同一命名互斥体（互斥体名逐字节
    相同，新旧进程互斥成立），串行化 undo_history.json / pending 文件的
    「读-改-写」，防多开/右键进程并发写 last-writer-wins 互覆丢批次。
    用法：with _history_lock() as (ok, reason): —— ok=False 表示 wait_ms 内
    没抢到锁，调用方必须放弃本次写入（绝不无锁写），稍后重试。
    句柄为局部变量、不触碰全局 _mutex_handle：本锁的调用线程（GUI 主线程）
    与散开/撤回工作线程不同，若共用全局句柄，工作线程释放时会误清本线程
    刚获的所有权（互斥体漏释放 → 其他进程最长阻塞 10 分钟）。
    所有权属于线程：同一线程内进出 with 块即可。"""
    ok, reason, owned = False, "", False
    h = None
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        h = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        if not h:
            reason = "无法创建散开互斥锁（CreateMutex 失败）。"
        else:
            wr = kernel32.WaitForSingleObject(h, wait_ms)
            if wr in (0x0, 0x80):  # WAIT_OBJECT_0 / WAIT_ABANDONED
                ok, owned = True, True
            else:  # WAIT_TIMEOUT：仍被他人持有
                reason = "另一个散开任务正在进行，请稍后重试。"
    except Exception as e:
        reason = f"散开互斥锁异常：{e}"
    try:
        yield ok, reason
    finally:
        if h:
            try:
                kernel32 = ctypes.windll.kernel32
                if owned:
                    kernel32.ReleaseMutex(ctypes.c_void_p(h))
                kernel32.CloseHandle(ctypes.c_void_p(h))
            except Exception:
                pass
