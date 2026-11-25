"""本地 IPC 服务器：接收右键静默散开进程发来的日志 / 撤回记录 / 父目录同步。

app-class-split 第 3 步从 App 上帝类抽出（原 _init_ipc / _ipc_* 一族），
行为冻结逐字搬移，仅 App 成员访问改写为构造注入的回调 / 内部属性。
红线三段原样保留：
  · _sockets set 保活（QLocalSocket 是 sip /Transfer/ 所有权对象，
    只靠信号闭包的循环引用会被 cyclic GC 整体回收并析构 C++ 对象，
    之后 readyRead 打到已删对象 → c0000005 无痕闪退）；
  · _buf 字节缓冲半包拼接（只对收齐到 \\n 的完整行 decode，
    防 UTF-8 半字符被替换成 U+FFFD 导致路径/撤回记录静默损坏）；
  · done 300ms 防抖合并（多选文件夹逐个发 done，
    不合并 = N 次 refresh 刷新风暴卡死 UI）。
"""

import json
import os

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtNetwork import QLocalServer

from core.guards import _is_protected_system_dir
from infra.paths import IPC_NAME
from ui.toast import ToastNotify


class IpcServer(QObject):
    """监听本地 socket 的 IPC 服务器。

    与 App 的交互收窄为构造注入的回调：日志 log、批次落盘 push_batch
    （A4：据返回值决定是否给静默端回 ack）、父目录预载
    current_dir / set_dir / refresh、任务栏进度
    taskbar_start / taskbar_ping / taskbar_done。
    若已有实例在监听（用户多开软件），本实例 listen 失败则降级为不监听，
    不影响自身使用。"""

    def __init__(self, parent=None, *, log, push_batch, is_local_busy,
                 is_remote_active, taskbar_start, taskbar_ping, taskbar_done,
                 is_root_dir, current_dir, set_dir, refresh):
        super().__init__(parent)
        self._log = log
        self._push_batch = push_batch
        self._is_local_busy = is_local_busy
        self._is_remote_active = is_remote_active
        self._taskbar_start = taskbar_start
        self._taskbar_ping = taskbar_ping
        self._taskbar_done = taskbar_done
        self._is_root_dir = is_root_dir
        self._current_dir = current_dir
        self._set_dir = set_dir
        self._refresh = refresh

        self.server = None
        self._buf = {}
        # A4：本会话内已落盘的批次 id（协议级幂等去重）——同一 batch_id 重复
        # 投递（静默端没等到 ack 重发）只回 ack 不再压栈
        self._seen = set()
        # 连接保活注册表：nextPendingConnection 的 socket 所有权归 Python（sip /Transfer/），
        # 局部变量 + 连接闭包只是"不可达循环引用"——cyclic GC 会回收并析构 C++ 对象，
        # 之后静默进程发日志、readyRead 打到已删 socket → c0000005 闪退（2026-08-27 实发，
        # 12 个右键进程同时连接、等锁未发数据的 socket 全部处于该状态被 GC 收割）。
        # 与「运行中的 QThread 必须有 Python 引用」同一类坑。必须放容器保活。
        self._sockets = set()
        # 右键散开 done 消息防抖：300ms 内合并多次 done，避免多选文件夹的刷新风暴
        self._done_pending = []
        self._done_timer = QTimer(self)
        self._done_timer.setSingleShot(True)
        self._done_timer.timeout.connect(self._flush_done)
        try:
            server = QLocalServer(self)
            server.newConnection.connect(self._on_new_connection)
            if server.listen(IPC_NAME):
                self.server = server
        except Exception:
            self.server = None

    def _on_new_connection(self):
        # newConnection 信号在多个连接同时到达时可能只发一次，
        # 必须 while 循环取空 pending 队列，否则 12 连接只接到第 1 个
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            if sock is None:
                break
            self._sockets.add(sock)
            sock.readyRead.connect(lambda s=sock: self._on_ready(s))
            sock.disconnected.connect(lambda s=sock: self._on_disconnect(s))

    def _on_disconnect(self, sock):
        # 断开才允许释放：先移缓冲再移集合，之后 GC 收集即安全（socket 已无事件来源）
        self._buf.pop(sock, None)
        self._sockets.discard(sock)
        # A4：已断开的 socket 回不了 ack，清掉 done 防抖缓冲里对它的引用
        # （既不占保活，也避免向已析构对象写数据）
        if self._done_pending:
            self._done_pending = [
                (pd, r, g, bid, None if s is sock else s)
                for pd, r, g, bid, s in self._done_pending
            ]

    def _on_ready(self, sock):
        # 用字节缓冲，只对收齐到 \n 的完整行做 decode；否则一次 readAll 的字节边界
        # 若落在中文（3 字节 UTF-8）字符中间，按文本解码会把半字符替换成 U+FFFD、
        # 原始字节丢失，导致路径/撤回记录静默损坏。半包字节留在缓冲等下次拼接。
        raw = self._buf.get(sock, b"") + bytes(sock.readAll())
        while b"\n" in raw:
            line, raw = raw.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            if len(line) > 2 * 1024 * 1024:
                continue  # 单行超限：丢弃该消息，防异常客户端灌大包
            try:
                self._handle(json.loads(line.decode("utf-8")), sock)
            except Exception:
                pass
        if len(raw) > 4 * 1024 * 1024:
            # 半包缓冲超限（只发数据不发换行）：断开连接自保
            self._buf.pop(sock, None)
            try:
                sock.abort()
            except Exception:
                pass
            return
        self._buf[sock] = raw

    def _ack(self, sock):
        """A4：批次已安全落盘后回应用层 ack（静默端收不到 ack 会降级写 pending）。"""
        try:
            if sock is not None:
                sock.write(b"ack\n")
                sock.flush()
        except Exception:
            pass  # 对端已断开：静默端超时降级 pending，启动合并按内容去重兜底

    def _handle(self, msg, sock=None):
        try:
            if not isinstance(msg, dict):
                return
            t = msg.get("type")
            if t == "log":
                # 本地未在执行散开时才把右键任务挂到任务栏进度条（避免与本地进度冲突）
                if not self._is_local_busy():
                    if not self._is_remote_active():
                        self._taskbar_start()
                    else:
                        self._taskbar_ping()  # 续命看门狗
                m = msg.get("msg")
                if isinstance(m, str):
                    self._log(m)
            elif t == "done":
                # 防抖：300ms 内多次 done（多选文件夹）合并成一次刷新，避免刷新风暴卡死 UI
                pd = msg.get("parent_dir")
                g = msg.get("group")
                bid = msg.get("batch_id")
                bid = bid if isinstance(bid, str) and bid else None
                # A4 幂等：本批已落盘（或已在防抖缓冲排队）的重复投递，
                # 直接回 ack 不再压栈（静默端等不到 ack 会重发）
                if bid and (bid in self._seen
                        or any(b == bid for _, _, _, b, _ in self._done_pending)):
                    self._ack(sock)
                    return
                recs = msg.get("undo_records")
                recs = [r for r in recs if isinstance(r, dict)
                        and isinstance(r.get("src"), str)
                        and isinstance(r.get("dst"), str)][:20000] \
                    if isinstance(recs, list) else []
                self._done_pending.append(
                    (pd if isinstance(pd, str) else None, recs,
                     g if isinstance(g, str) else None, bid, sock))
                self._done_timer.start(300)
        except Exception as e:
            # 不再裸吞：IPC 同步失败（刷新/压栈/Toast 等）至少要能在日志区看到
            try:
                self._log(f"[错误] 处理右键同步消息失败：{e}")
            except Exception:
                pass

    def _flush_done(self):
        """防抖定时器触发：把 300ms 内缓冲的多个右键 done 合并成一次刷新。

        多选文件夹右键散开时，每个文件夹处理完会发一次 done；若不合并，
        50 个文件夹 = 50 次 refresh()，瞬间卡死 UI。此处只刷新一次、压栈一次、提示一次。
        """
        try:
            self._flush_done_impl()
        except Exception as e:
            try:
                self._log(f"[错误] 处理右键完成同步失败：{e}")
            except Exception:
                pass

    def _flush_done_impl(self):
        pend = self._done_pending
        self._done_pending = []
        if not pend:
            return
        parents = []
        batches = []  # [(group, recs, acks)] 相邻同组的记录合并，一次右键操作最终只压一批
        for pd, r, g, bid, s in pend:
            if pd and isinstance(pd, str) and os.path.isdir(pd):
                # 根目录/系统目录不跳转不刷新（refresh 会弹模态框，定时器路径禁止）
                if self._is_root_dir(pd) or _is_protected_system_dir(pd):
                    self._log(f"[警告] 右键散开的父目录受保护，不自动跳转：{pd}")
                elif pd not in parents:
                    parents.append(pd)  # 保留出现顺序，取首个有效父目录做跳转/刷新
            if r:
                if g and batches and batches[-1][0] == g:
                    batches[-1][1].extend(r)
                    batches[-1][2].append((bid, s))
                else:
                    batches.append((g, list(r), [(bid, s)]))
        if parents:
            unique = list(dict.fromkeys(parents))  # 去重保序
            # 只刷新一次：当前正在查看的父目录命中则原地刷新；否则跳到第一个父目录。
            # 旧实现逐个父目录 setText+refresh——N-1 次刷新不可见纯浪费，
            # 且 refresh 串行起扫描会在多父目录多选时卡死 UI。
            cur_text = self._current_dir()
            current = os.path.normcase(os.path.normpath(cur_text)) if cur_text else ""
            target = None
            for p in unique:
                if os.path.normcase(os.path.normpath(p)) == current:
                    target = p
                    break
            if target is None:
                target = unique[0]
                self._set_dir(target)
            self._refresh()
        for g, recs, acks in batches:
            # A4：只有真正落盘（压栈 + 保存成功）才回 ack；忙碌延后/保存暂缓
            # 均不回——静默端超时降级写 pending，启动合并按内容去重兜底
            bids = [b for b, _ in acks if b]
            if self._push_batch(recs, g, bids):
                for bid, s in acks:
                    if bid:
                        self._seen.add(bid)
                    self._ack(s)
        self._log("[右键] 右键散开任务已完成，可在本软件内撤回。")
        # 自定义 Toast 替代托盘气泡（不依赖 Windows 通知中心）
        ToastNotify("右键散开任务已完成。").show()
        # 仅在本地未执行散开时才动任务栏进度，避免覆盖本地 Worker 的真实进度
        if not self._is_local_busy():
            self._taskbar_done()

    def shutdown(self):
        """退出前关停：先把 300ms 防抖窗内已收的 done 批次同步落栈/回 ack
        （进程退出后定时器不会再触发，不冲刷这批记录就只能靠静默端 pending
        兜底了），再停定时器、关闭监听。幂等，任何失败不影响退出流程。"""
        try:
            self._done_timer.stop()
            if self._done_pending:
                self._flush_done()
        except Exception:
            pass
        try:
            if self.server is not None:
                self.server.close()
        except Exception:
            pass
