import os
import time
import ctypes
import send2trash

from PyQt6.QtCore import QThread, pyqtSignal
from infra.fileutil import _unique_dst
from infra.paths import CRASH_LOG
from core.fileops import _fast_move_file
from core.locking import _acquire_scatter_lock, _release_scatter_lock
from backup.trust import _is_trusted_backup, _is_undo_path_allowed
from backup.vault import _rmdir_backup_ancestors


class UndoWorker(QThread):
    """在子线程中执行撤回（还原一批散开记录），避免大批量撤回在 GUI 线程卡死界面。
    命名互斥体所有权属于线程，因此由本线程在 run() 开头自行获取、结束自释放；
    获取失败（他人持有）发 lockfailed，App 保持撤回栈原样。"""

    logmsg = pyqtSignal(str)
    finishedsig = pyqtSignal()
    lockfailed = pyqtSignal(str)

    def __init__(self, batch):
        super().__init__()
        self.batch = batch
        self.failed = []   # 还原失败的记录（如文件被占用），主线程重新压栈可再试
        self._stop = False

    def request_stop(self):
        """退出流程调用：在下一条记录边界安全停下（不中断单条搬移）。"""
        self._stop = True

    def _drop_rec(self, rec):
        """把已处理完（成功或不可重试）的记录从 remaining 移除；
        失败可重试的记录不调本方法，留在 remaining 供再次撤回。"""
        for j in range(len(self.remaining) - 1, -1, -1):
            if self.remaining[j] is rec:
                del self.remaining[j]
                break

    def run(self):
        # COM 保险：send2trash 等 shell 调用在部分环境需要已初始化的 COM apartment
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 2)
        except Exception:
            pass
        _lock_ok, _lock_reason = _acquire_scatter_lock()
        if not _lock_ok:
            self.lockfailed.emit(_lock_reason)
            return
        self.remaining = list(self.batch)  # 随处理进度收缩；结束仍剩的 = 失败/未完成
        log_buf = []

        def b_log(msg):
            # 与 Worker 同款攒批：错误/警告实时发，普通还原记录 50 条一批
            if msg.startswith(("[错误]", "[警告]")):
                if log_buf:
                    self.logmsg.emit("\n".join(log_buf))
                    log_buf.clear()
                self.logmsg.emit(msg)
                return
            log_buf.append(msg)
            if len(log_buf) >= 50:
                self.logmsg.emit("\n".join(log_buf))
                log_buf.clear()

        bak_dirs = set()  # 本次撤回动过的备份目录，结束时空目录清掉

        try:
            # 提前声明本批旧文件的保护方式：有备份的自动还原，无备份的需手动找回
            if any(isinstance(r, dict) and r.get("backup") for r in self.batch):
                b_log("[撤回] 本次操作包含覆盖备份：被覆盖的旧文件将自动还原。")
            if any(isinstance(r, dict) and r.get("overwrote") for r in self.batch):
                b_log("[警告] 本次包含没有备份的覆盖：旧文件无法自动还原。")
            for rec in reversed(self.batch):
                if self._stop:
                    b_log("[停止] 用户已中止撤回，未完成项保留在撤回栈。")
                    break
                # 畸形记录容错：结构/路径缺失只警告跳过，绝不让异常逃出线程
                # （PyQt6 子线程未捕获异常会直接终止整个进程 = 闪退）
                if not isinstance(rec, dict):
                    b_log(f"[警告] 跳过畸形撤回记录：{rec!r}")
                    self._drop_rec(rec)
                    continue
                src = rec.get("src")   # 原始位置
                dst = rec.get("dst")   # 当前位置
                if not isinstance(src, str) or not src or \
                        not isinstance(dst, str) or not dst:
                    b_log(f"[警告] 跳过缺路径的撤回记录：{rec!r}")
                    self._drop_rec(rec)
                    continue
                # 路径校验：记录可来自 JSON/IPC，不可全信；拒绝根目录与系统保护目录
                if not _is_undo_path_allowed(src) or not _is_undo_path_allowed(dst):
                    b_log(f"[警告] 跳过可疑撤回记录（命中根目录/系统保护目录）: "
                          f"{src} -> {dst}")
                    self._drop_rec(rec)
                    continue
                mode = rec.get("mode", "move")
                try:
                    if mode == "move":
                        bak = rec.get("backup")
                        if bak is not None and not _is_trusted_backup(bak):
                            b_log(f"[警告] 忽略不属于备份目录的异常 backup 路径: {bak}")
                            bak = None
                        if os.path.exists(dst):
                            os.makedirs(os.path.dirname(src), exist_ok=True)
                            if os.path.exists(src):
                                # 原位被占（如重复撤回）：改名还原，不物理删除现有文件
                                ns = _unique_dst(src)
                                _fast_move_file(dst, ns)
                                b_log(f"[警告] 还原位置被占用，已改名还原: {ns}")
                            else:
                                _fast_move_file(dst, src)
                                b_log(f"[撤回] 已还原: {src}")
                        else:
                            # dst 已被前一条记录搬走或不存在：仍重建原始文件夹骨架
                            # （否则撤回后整个文件夹凭空消失）
                            try:
                                os.makedirs(os.path.dirname(src), exist_ok=True)
                            except Exception:
                                pass
                            if os.path.exists(src):
                                # A3 幂等重入：dst 已不在散开位置而 src 已在原位
                                # = 上次已成功还原（重复撤回/崩溃重试在途批次），
                                # 静默跳过，不误报「无法自动还原」
                                pass
                            elif isinstance(bak, str) and bak and os.path.isfile(bak):
                                b_log(f"[警告] {os.path.basename(src)} 已不在散开位置，"
                                      f"无法还原新文件；旧文件备份将在下方还原。")
                            elif rec.get("overwrote"):
                                # 遗留记录：旧文件在回收站，明确指出找哪个、放回哪里
                                b_log(f"[警告] 无法自动还原：{os.path.basename(src)} "
                                      f"（被同名文件覆盖，请从回收站找回并放回 "
                                      f"{os.path.dirname(src)}）")
                            else:
                                b_log(f"[警告] 无法自动还原：{os.path.basename(src)} "
                                      f"（文件已不在散开位置: {dst}）")
                        # 还原被覆盖的旧文件：备份放回 dst（它散开前的原位）
                        if isinstance(bak, str) and bak and os.path.isfile(bak):
                            bak_dirs.add(os.path.dirname(bak))
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            if os.path.exists(dst):
                                nb = _unique_dst(dst)
                                _fast_move_file(bak, nb)
                                b_log(f"[警告] 旧文件还原位置被占用，已改名还原: {nb}")
                            else:
                                _fast_move_file(bak, dst)
                                b_log(f"[撤回] 已还原被覆盖的旧文件: {dst}")
                        elif isinstance(bak, str) and bak:
                            # 记录里有备份但文件已不在（超期清理/手动删除/盘未挂载）：
                            # 不能静默跳过，须明确告知旧文件无法自动还原
                            b_log(f"[警告] 旧文件备份已不存在，无法自动还原: "
                                  f"{os.path.basename(dst)}"
                                  f"（备份原路径: {bak}）")
                    else:  # 复制模式：副本送回收站（可找回），不再物理删除
                        bak = rec.get("backup")
                        if bak is not None and not _is_trusted_backup(bak):
                            b_log(f"[警告] 忽略不属于备份目录的异常 backup 路径: {bak}")
                            bak = None
                        has_bak = isinstance(bak, str) and bak and os.path.isfile(bak)
                        if os.path.exists(dst):
                            send2trash.send2trash(dst)
                            b_log(f"[撤回] 已将副本送入回收站: {dst}")
                        # A3 幂等重入：副本已不在（上次撤回已送回收站/用户已自行删除）
                        # 视为已处理，静默跳过不告警；有备份的旧文件仍在下方照常还原
                        # 还原被覆盖的旧文件：备份放回 dst（它被覆盖前的原位）
                        if has_bak:
                            bak_dirs.add(os.path.dirname(bak))
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            if os.path.exists(dst):
                                nb = _unique_dst(dst)
                                _fast_move_file(bak, nb)
                                b_log(f"[警告] 旧文件还原位置被占用，已改名还原: {nb}")
                            else:
                                _fast_move_file(bak, dst)
                                b_log(f"[撤回] 已还原被覆盖的旧文件: {dst}")
                        elif isinstance(bak, str) and bak:
                            # 与 move 分支同款：备份丢失不静默，明确告知
                            b_log(f"[警告] 旧文件备份已不存在，无法自动还原: "
                                  f"{os.path.basename(dst)}"
                                  f"（备份原路径: {bak}）")
                    self._drop_rec(rec)  # 成功/不可重试：出列；异常走 except 保留重试
                except Exception as e:
                    self.failed.append(rec)
                    b_log(f"[错误] 撤回失败 {src}: {e}")
            # 清理已清空的备份目录；还有剩余备份的保留充当保护，不自动删
            # B4：备份落点带相对层级，逐级向上清掉空子目录/批次目录/空根
            for bd in bak_dirs:
                _rmdir_backup_ancestors(bd)
        except Exception:
            # 整线程兜底：traceback 写 crash.log（下次启动日志区展示），
            # 绝不让未捕获异常静默闪退
            import traceback as _tb
            _txt = _tb.format_exc()
            try:
                with open(CRASH_LOG, "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 撤回过程异常\n{_txt}\n")
            except Exception:
                pass
            b_log(f"[错误] 撤回过程出现内部异常，已记录到 crash.log：\n{_txt}")
        finally:
            if log_buf:
                self.logmsg.emit("\n".join(log_buf))
            self.logmsg.emit("[撤回] 还原完成。")
            _release_scatter_lock()
            self.finishedsig.emit()
