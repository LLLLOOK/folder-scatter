import os
import shutil
import ctypes
import threading

from PyQt6.QtWidgets import QFileIconProvider, QTreeWidgetItem

from PyQt6.QtCore import QThread, Qt, pyqtSignal
from core.fileops import count_files
from core.locking import _ScatterBusyError
from core.scatter import _expand_selected_by_depth, execute_scatter
from backup.vault import _new_backup_batch
from ui.widgets import _list_entries, build_tree_item


class Worker(QThread):
    """在子线程中执行散开，避免界面卡死；支持安全停止。"""
    logmsg = pyqtSignal(str)
    progresssig = pyqtSignal(int)
    skipsig = pyqtSignal(int, int, int)
    finishedsig = pyqtSignal()
    maxsig = pyqtSignal(int)

    def __init__(self, selected, parent_dir, mode, conflict,
                 suffixes, max_size_mb, simulate, skip_hidden=False,
                 to_trash=False, level="one", suffix_exclude=False):
        super().__init__()
        self.selected = selected
        self.parent_dir = parent_dir
        self.mode = mode
        self.conflict = conflict
        self.suffixes = suffixes
        self.max_size_mb = max_size_mb
        self.simulate = simulate
        self.skip_hidden = skip_hidden
        self.to_trash = to_trash
        self.level = level
        self.suffix_exclude = suffix_exclude
        self.backup_batch = _new_backup_batch()  # 本次操作备份的批次子目录名
        self.undo_records = []
        self._stop_event = threading.Event()
        # 完成摘要统计（供 _on_worker_finished 状态栏汇总；[错误] 行每行对应一个失败项）
        self.moved_count = 0
        self.folders_count = 0
        self.err_count = 0
        self.last_skip_stats = [0, 0, 0]  # 大文件跳过 / 被占用 / 权限不足（结束时快照）

    def request_stop(self):
        self._stop_event.set()

    def run(self):
        # COM 保险：send2trash 等 shell 调用在部分环境需要已初始化的 COM apartment
        # （已初始化返回 S_FALSE 无害；线程退出由系统回收，与 TaskbarProgress 同款）
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 2)
        except Exception:
            pass
        # === 优化：日志/进度按批发送，避免万级文件时每条信号都跨线程 + 重绘 QTextEdit ===
        log_buf = []
        last_prog = [0]
        last_skip = [0, 0, 0]  # 大文件跳过、被占用、权限不足
        LOG_BATCH = 50
        PROG_BATCH = 50

        def b_log(msg):
            # 错误/跳过实时单列（保证颜色正确且不延迟），其余攒批合并减少信号量
            if msg.startswith("[错误]"):
                self.err_count += 1  # 每个失败项恰好一行 [错误]，供完成摘要统计
            if msg.startswith(("[错误]", "[跳过]")):
                if log_buf:
                    self.logmsg.emit("\n".join(log_buf))
                    log_buf.clear()
                self.logmsg.emit(msg)
                return
            log_buf.append(msg)
            if len(log_buf) >= LOG_BATCH:
                self.logmsg.emit("\n".join(log_buf))
                log_buf.clear()

        def b_prog(n):
            if n - last_prog[0] >= PROG_BATCH:
                self.progresssig.emit(n)
                last_prog[0] = n

        def b_skip(skipped, occupied, permission):
            last_skip[0] = skipped
            last_skip[1] = occupied
            last_skip[2] = permission

        # 先按散开深度在后台展开勾选的文件夹（深度不改目录树勾选，展开只发生在执行期）
        try:
            actual_selected = _expand_selected_by_depth(self.selected, self.level)
        except Exception as e:
            b_log(f"[错误] 展开散开源失败：{e}")
            actual_selected = []
        size_acc = [] if (self.mode == "copy" and not self.simulate) else None
        try:
            # 进度上限 = 展开后每个源文件夹「直接文件数」之和：散开是把每个源上提一层，
            # 实际移动的就是各源的直接文件，故按 recursive=False 逐个统计才与真实工作量一致
            # （旧实现按未展开列表递归计数，"包含子文件夹"深度会把更深层也算进去导致上限偏高）
            total = 0
            for p in actual_selected:
                total += count_files(p, False, stop=self._stop_event.is_set,
                                     size_acc=size_acc)
            self.maxsig.emit(max(1, total))
        except Exception:
            self.maxsig.emit(1)
        # copy 模式磁盘空间启发式预检：总量超过目标分区剩余空间时提前警告
        # （仅警告不中止；失败文件照常打错误日志，已复制部分可用「撤回」清理）
        if size_acc is not None:
            try:
                need = sum(size_acc)
                free = shutil.disk_usage(self.parent_dir).free
                if need > free:
                    b_log("[警告] 目标分区剩余空间可能不足：预计复制 "
                          f"{need/1024/1024:.0f}MB，剩余 {free/1024/1024:.0f}MB。"
                          "将继续执行；若中途失败，可用「撤回」清理已复制文件。")
            except OSError:
                pass
        try:
            _res = execute_scatter(
                actual_selected, self.parent_dir, self.mode, self.conflict,
                self.suffixes, self.max_size_mb, self.simulate,
                b_log, b_prog, b_skip,
                self._stop_event.is_set, self.undo_records,
                skip_hidden=self.skip_hidden, to_trash=self.to_trash,
                suffix_exclude=self.suffix_exclude,
                backup_batch=self.backup_batch)
            if _res:
                self.moved_count, self.folders_count = _res
        except _ScatterBusyError:
            pass  # 锁冲突原因已在 execute_scatter 内用 [错误] 打出，无需重复
        except Exception:
            import traceback
            self.logmsg.emit("[错误] 执行异常：" + traceback.format_exc().splitlines()[-1])
        finally:
            self.last_skip_stats = list(last_skip)  # 快照给完成摘要（GUI 线程在 finishedsig 后读）
            if log_buf:
                self.logmsg.emit("\n".join(log_buf))
            if last_prog[0] > 0:
                self.progresssig.emit(last_prog[0])
            if last_skip[0] > 0 or last_skip[1] > 0 or last_skip[2] > 0:
                self.skipsig.emit(last_skip[0], last_skip[1], last_skip[2])
            self.finishedsig.emit()


class TreeScanWorker(QThread):
    """后台线程扫描目录并构建整棵 QTreeWidgetItem 树（含图标），
    然后把『顶级节点』分批发给主线程，由主线程用 addTopLevelItems 批量添加。
    这样几百个文件夹时，耗时的 os 扫描 + item 创建都在子线程完成，
    主线程只负责『攒一批 → 一次性灌入 → 重绘一次』，界面不再卡死。

    代际（gen）+ abort：refresh 再次触发时不再 quit+wait 硬等旧线程
    （quit 对无事件循环的线程本就没有作用，wait 会让巨型目录卡死 UI），
    而是给旧线程打 abort 标记、直接起新线程；旧线程若仍发出信号，
    主线程按 gen 比对直接丢弃。"""
    items_ready = pyqtSignal(list, int)  # 一批顶级 QTreeWidgetItem + 代际号
    scan_finished = pyqtSignal(int)
    scan_error = pyqtSignal(str, int)
    BATCH_SIZE = 200                 # 每批最多 200 个顶级节点

    def __init__(self, base_path, gen):
        super().__init__()
        self.base_path = base_path
        self.gen = gen
        self._abort = False

    def abort(self):
        self._abort = True

    def run(self):
        # COM 保险：图标抓取（SHGetFileInfo）在部分环境需要已初始化的 COM apartment
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 2)
        except Exception:
            pass
        try:
            icon_provider = QFileIconProvider()  # 在子线程内创建，避免跨线程取图标
            dirs, files = _list_entries(self.base_path)
            batch = []
            for name in dirs + files:
                if self._abort:
                    return  # 已被新一轮刷新取代：结果直接丢弃，不再发任何信号
                try:
                    batch.append(build_tree_item(
                        name, self.base_path, icon_provider,
                        stop=lambda: self._abort))
                except Exception:
                    # 单个坏/深层目录不再拖垮整次扫描：退化为叶子节点继续
                    it = QTreeWidgetItem()
                    it.setText(0, name)
                    full = os.path.join(self.base_path, name)
                    it.setData(0, Qt.ItemDataRole.UserRole,
                               {"path": full, "is_dir": os.path.isdir(full)})
                    batch.append(it)
                if len(batch) >= self.BATCH_SIZE:
                    self.items_ready.emit(batch, self.gen)
                    batch = []
            if batch:
                self.items_ready.emit(batch, self.gen)
            self.scan_finished.emit(self.gen)
        except Exception:
            import traceback
            self.scan_error.emit(traceback.format_exc().splitlines()[-1], self.gen)
