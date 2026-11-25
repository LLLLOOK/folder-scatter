"""撤回栈存储（UndoHistoryStore）：撤回栈的唯一拥有者。

从 ui/main_window.py 的 App 类抽出的撤回历史管理逻辑（第二轮拆分第 1 步），
方法体与原 App 实现逐字一致（只搬不改）。纯 Python、不 import Qt：
日志与「撤回按钮可用态」经构造函数注入的回调触达 UI，因此可被 tests 直接单测。

数据文件落点不变（infra.paths 常量）：undo_history.json / pending / inflight
仍在程序根目录。
"""

import json
import os

from core.locking import _history_lock
from infra.paths import (UNDO_HISTORY_FILE, UNDO_HISTORY_MAX_BATCHES,
                         UNDO_INFLIGHT_FILE, UNDO_PENDING_FILE)
from backup.vault import _purge_batch_backups
from undo.store import (_backup_undo_json, _clear_inflight,
                        _read_undo_json_safe)


class UndoHistoryStore:
    """撤回栈的唯一拥有者：栈的增删/截断（20 批）/落盘（持锁+.bak+原子写）/
    启动载入（pending 合并 + inflight 自愈 + 备份缺失自愈）/延后批冲刷。

    回调注入：
    - log(msg)：打日志（App 传 self.log_msg）。
    - set_undo_enabled(b)：点亮/熄灭撤回按钮（App 传 btn_undo.setEnabled 的转发；
      缺省为空操作，便于无 UI 单测）。
    """

    def __init__(self, log, set_undo_enabled=None):
        self._log = log
        self._set_undo_enabled = set_undo_enabled or (lambda b: None)
        self.stack = []          # 原 App.undo_stack
        self.last_group = None   # 原 App._last_undo_group
        self.deferred = []       # 原 App._deferred_undo

    # ---------- 压栈 / 延后批 ----------

    def push_batch(self, recs, group, busy, batch_ids=()):
        """压入一批右键撤回记录。group 非空且与栈顶批次同组 → 合并进栈顶批次
        （一次右键多选操作 = 一批撤回，只占 20 步上限里的 1 步）；
        busy=True（本地散开/撤回进行中，由调用方判定）则延后压栈
        （不在运行中点亮撤回按钮）。
        batch_ids：本批携带的 A4 批次 id 集合（IPC 防抖合并后可能多个）。
        F28：延后队列按它幂等——静默端没等到 ack 会换连接重发同批，
        不去重会在退出冲刷/忙碌结束统一压栈时把同一批记录压栈两次。
        重复投递直接丢弃并返回 False（不回 ack：首份仍未落盘，
        静默端超时降级 pending，启动合并按内容去重兜底，不丢记录）。
        返回是否已安全落盘（A4：调用方据此决定是否给静默端回 ack；
        忙碌延后/保存暂缓返回 False，静默端超时降级写 pending）。"""
        if not busy:
            if group and group == self.last_group and self.stack:
                self.stack[-1].extend(recs)
            else:
                self.stack.append(recs)
            self.last_group = group
            self._set_undo_enabled(True)
            return self.save()  # 持久化右键散开的撤回批次
        # F28：任一携带的 bid 已在延后队列 = 同批重复投递，丢弃
        if batch_ids and any(
                bid and bid in old_bids
                for _, _, old_bids in self.deferred
                for bid in batch_ids):
            return False
        self.deferred.append((recs, group, tuple(batch_ids)))
        return False

    def flush_deferred(self):
        """退出前兜底：散开/撤回进行中收到的右键批次挂在 deferred，
        直接退出会让这批撤回记录永久丢失（IPC 已送达，pending 文件无副本）——
        绕开 push_batch 的忙碌判定直接落栈并持久化。"""
        pend = self.deferred
        if not pend:
            return
        for recs, group, _bids in pend:
            if group and group == self.last_group and self.stack:
                self.stack[-1].extend(recs)
            else:
                self.stack.append(recs)
            self.last_group = group
        self.deferred = []
        self.save()

    def persist_worker_batch(self, worker):
        """退出兜底：本地散开进行中退出时，finishedsig 槽来不及执行，
        已完成文件的撤回记录会随退出丢失——_shutdown_workers 等停后直接落栈。
        worker 非 None 即表示完成槽没跑过（_on_worker_finished 正常完成会置 None）。
        须在 _shutdown_workers 之后调用（线程停稳再读 undo_records）。"""
        w = worker
        if w is None or w.isRunning() or w.simulate or not w.undo_records:
            return
        self.stack.append(list(w.undo_records))
        self.last_group = None  # 本地散开批次无分组，阻断后续右键批次误并
        if self.save():
            _clear_inflight()  # B1：批次已安全落盘，清增量账本

    # ---------- 落盘 ----------

    def save(self, wait_ms=3000):
        """将撤回栈持久化到 undo_history.json，支持跨会话（隔天）撤回。
        原子写入：先写 .tmp 再 os.replace，避免写过程中断电/崩溃导致 JSON 损坏。
        只保留最近 UNDO_HISTORY_MAX_BATCHES 批（丢最旧），防止文件无限膨胀。
        写盘持锁（A2 移植）：undo_history.json 是多进程共享文件，无锁并发写
        last-writer-wins 互覆丢批次；抢不到锁则本次不落地（绝不无锁写），
        内存栈保留，随下一次保存/退出兜底重试。
        wait_ms：等锁上限。历史锁与散开互斥体同名共用，散开/撤回工作线程
        会持锁整个执行期——主线程在任务进行中触发的保存（如清空备份联动）
        必须传短超时快速失败，而不是冻结 UI 3 秒（F5）。
        返回是否已落盘（A4：IPC 批次只有落盘成功才给静默端回 ack）。"""
        with _history_lock(wait_ms) as (_lock_ok, _lock_reason):
            if not _lock_ok:
                self._log(f"[警告] 撤回历史保存暂缓（{_lock_reason}），"
                          "将随下次操作重试。")
                return False
            try:
                if len(self.stack) > UNDO_HISTORY_MAX_BATCHES:
                    dropped = self.stack[:-UNDO_HISTORY_MAX_BATCHES]
                    self.stack = self.stack[-UNDO_HISTORY_MAX_BATCHES:]
                    _purge_batch_backups(dropped)  # 被淘汰批次不可再撤回，备份随删
                _backup_undo_json(UNDO_HISTORY_FILE)  # B2：覆盖写前留 .bak 双保险
                tmp = UNDO_HISTORY_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.stack, f, ensure_ascii=False, indent=2)
                os.replace(tmp, UNDO_HISTORY_FILE)  # 同盘原子替换，读者不会看到半截 JSON
                return True
            except Exception as e:
                self._log(f"[警告] 撤回历史保存失败：{e}")
                return False

    # ---------- 启动载入 ----------

    def load_on_startup(self):
        """启动时从 undo_history.json 恢复撤回栈，使上次的操作仍可撤回。"""
        try:
            # 先合并 GUI 关闭期间右键产生的 pending 撤回批次（合并成功后才删 pending 文件，
            # 失败则保留待下次启动重试——否则合并失败还删文件会让右键撤回批次永久丢失）
            if os.path.exists(UNDO_PENDING_FILE):
                # 合并是「读 pending + 读主文件 + 写主文件 + 删 pending」的读-改-写：
                # 与静默进程的 pending 写入共用同一把锁串行化（A2 移植），
                # 否则启动合并与右键暂存并发互踩丢批次；抢不到锁则不合并
                # （绝不无锁写），pending 保留待下次启动重试
                with _history_lock() as (_mok, _mreason):
                    if not _mok:
                        self._log(f"[警告] 右键撤回暂存合并暂缓（{_mreason}），"
                                  "暂存文件已保留，下次启动重试。")
                    else:
                        merged_ok = False
                        try:
                            with open(UNDO_PENDING_FILE, "r", encoding="utf-8") as f:
                                pend = json.load(f)
                            if isinstance(pend, list) and pend:
                                base = []
                                if os.path.exists(UNDO_HISTORY_FILE):
                                    # B2：主文件损坏先试 .bak、双坏隔离 .corrupt，
                                    # 不得静默以空底合并把旧历史覆盖丢光
                                    _bdata, _bissues = _read_undo_json_safe(
                                        UNDO_HISTORY_FILE)
                                    for _m in _bissues:
                                        self._log(_m)
                                    if isinstance(_bdata, list):
                                        base = _bdata
                                # A4 去重：静默端没等到 ack 时同批可能两处落盘
                                # （GUI 已压栈保存 + 静默端又写了 pending；或 GUI 崩溃在
                                # 「已落盘未回 ack」之间后重发）——按内容跳过与现有
                                # 历史（含前面已并入的暂存批）完全重复的暂存批次
                                _seen_keys = set()

                                def _rec_keys_of(b):
                                    _rs = b.get("records") if isinstance(b, dict) else b
                                    if isinstance(_rs, list):
                                        return [json.dumps(_r, sort_keys=True,
                                                           ensure_ascii=False)
                                                for _r in _rs if isinstance(_r, dict)]
                                    return []

                                for _b in base:
                                    _seen_keys.update(_rec_keys_of(_b))
                                _kept = []
                                for _e in pend:
                                    _ks = _rec_keys_of(_e)
                                    if _ks and all(_k in _seen_keys for _k in _ks):
                                        continue  # 内容重复批次（重复落盘的同批）
                                    _kept.append(_e)
                                    _seen_keys.update(_ks)
                                if len(_kept) < len(pend):
                                    self._log(
                                        f"[撤回] 已跳过 {len(pend) - len(_kept)} 批"
                                        "与现有历史重复的右键暂存记录。")
                                base.extend(_kept)
                                # 与 save 同款：写前留 .bak（B2）
                                # + 先 .tmp 再 replace 原子写，防半截 JSON
                                _backup_undo_json(UNDO_HISTORY_FILE)
                                tmp = UNDO_HISTORY_FILE + ".tmp"
                                with open(tmp, "w", encoding="utf-8") as f:
                                    json.dump(base, f, ensure_ascii=False, indent=2)
                                os.replace(tmp, UNDO_HISTORY_FILE)
                            merged_ok = True
                        except Exception as e:
                            self._log(f"[警告] 右键撤回暂存合并失败（已保留暂存文件，下次启动重试）：{e}")
                        if merged_ok:
                            try:
                                os.remove(UNDO_PENDING_FILE)
                            except Exception:
                                pass
            if os.path.exists(UNDO_HISTORY_FILE):
                # B2：主文件损坏先试 .bak、双坏隔离 .corrupt 并醒目告警，
                # 绝不静默置空（旧行为：json.load 失败被吞，全部可撤回批次无声消失）
                data, _issues = _read_undo_json_safe(UNDO_HISTORY_FILE)
                for _m in _issues:
                    self._log(_m)
                if isinstance(data, list):
                    # 兼容历史结构：纯列表批次原样保留；右键分组批次是
                    # {"group":…,"records":[…]} 字典，解包取其记录列表
                    _norm = []
                    for b in data:
                        if isinstance(b, dict) and isinstance(b.get("records"), list):
                            _norm.append(b["records"])
                        elif isinstance(b, list):
                            _norm.append(b)
                        else:
                            _norm.append([b])
                    self.stack = _norm
                    self.last_group = None  # 跨会话不续组，防新右键操作误并旧批次
                    if len(self.stack) > UNDO_HISTORY_MAX_BATCHES:
                        dropped = self.stack[:-UNDO_HISTORY_MAX_BATCHES]
                        self.stack = self.stack[-UNDO_HISTORY_MAX_BATCHES:]
                        _purge_batch_backups(dropped)  # 截断批次不可再撤回，备份随删
                    # 备份缺失自愈：批次的备份文件已不存在（被清空/手动删除/
                    # 旧版本清空时未同步撤回栈），还原旧文件已无保障 → 整批移出，
                    # 与「清空备份」按钮同一策略；纯移动批次无 backup 字段不受影响
                    keep, lost = [], 0
                    for b in self.stack:
                        gone = False
                        for rec in b if isinstance(b, list) else []:
                            bak = rec.get("backup") if isinstance(rec, dict) else None
                            if isinstance(bak, str) and not os.path.isfile(bak):
                                # 盘符不存在（U盘/移动盘未插）视为离线而非丢失：
                                # 保留批次，待盘挂载后仍可撤回，不做永久出栈
                                _drv = os.path.splitdrive(bak)[0]
                                if _drv and not os.path.exists(_drv + os.sep):
                                    continue
                                gone = True
                                break
                        if gone:
                            lost += 1
                        else:
                            keep.append(b)
                    if lost:
                        self.stack = keep
                        self.save()
                        self._log(f"[撤回] {lost} 批操作的备份已不存在，"
                                  "已移出撤回列表。")
                    self._set_undo_enabled(bool(self.stack))
                    if self.stack:
                        self._log(
                            f"[撤回] 已载入上次的 {len(self.stack)} 批可撤回操作。")
            # B1 自愈：上次散开进行中崩溃/断电残留的 inflight 增量账本
            # → 合成独立批次入栈（无分组，不参与并组），落盘成功后清账
            if os.path.exists(UNDO_INFLIGHT_FILE):
                try:
                    with open(UNDO_INFLIGHT_FILE, "r", encoding="utf-8") as f:
                        _inflight = json.load(f)
                except Exception as e:
                    _inflight = None
                    self._log(f"[警告] 未完成的散开记录文件损坏，已清除：{e}")
                if isinstance(_inflight, list) and _inflight:
                    # 与 pending 合并同款内容去重：记录实际已落盘（上次清账失败）
                    # 的残留不重复入栈
                    _exist = set()
                    for _b in self.stack:
                        for _r in (_b if isinstance(_b, list) else []):
                            if isinstance(_r, dict):
                                _exist.add(json.dumps(_r, sort_keys=True,
                                                      ensure_ascii=False))
                    _iks = [json.dumps(_r, sort_keys=True, ensure_ascii=False)
                            for _r in _inflight if isinstance(_r, dict)]
                    if _iks and all(_k in _exist for _k in _iks):
                        _clear_inflight()  # 已落盘过的残留，直接清账
                    else:
                        self.stack.append(_inflight)
                        self.last_group = None  # 独立批次，防误并
                        if self.save():
                            _clear_inflight()
                        self._set_undo_enabled(True)
                        self._log(
                            f"[撤回] 检测到上次未完成的散开（{len(_inflight)} 条记录），"
                            "已恢复为可撤回批次。")
                else:
                    _clear_inflight()  # 空/损坏账本：无价值，清掉
        except Exception as e:
            self._log(f"[警告] 撤回历史载入失败：{e}")

    # ---------- 备份联动 ----------

    def invalidate_backups(self, root):
        """某备份根被清空后调用：引用这些备份的撤回批次整批移出栈。
        旧文件已无法还原，留半批只会撤回出残缺目录，故整批移除。
        纯移动批次（无 backup 字段）不受影响。返回移除的批数。
        F5：本方法可由设置对话框在散开/撤回进行中触发（主线程）——
        历史锁与散开互斥体同名共用，工作线程持锁整个执行期，等锁必须
        短超时快速失败而非冻结 UI 3 秒；落盘失败的窗口期由两道兜底收敛：
        下一次 save 写入内存栈；若直到退出都没再保存，启动载入的
        「备份缺失自愈」会把引用已删备份的批次再次出栈。"""
        prefix = os.path.normcase(os.path.abspath(root)).rstrip(os.sep) + os.sep
        keep, dropped = [], 0
        for batch in self.stack:
            hit = False
            if isinstance(batch, list):
                for rec in batch:
                    bak = rec.get("backup") if isinstance(rec, dict) else None
                    if isinstance(bak, str) and os.path.normcase(
                            os.path.abspath(bak)).startswith(prefix):
                        hit = True
                        break
            if hit:
                dropped += 1
            else:
                keep.append(batch)
        if dropped:
            self.stack = keep
            self.last_group = None  # 栈结构已变，阻断后续右键批次误并
            if not self.save(wait_ms=250):  # F5：主线程快速失败，不冻结 UI
                self._log("[警告] 撤回历史暂未落盘（散开/撤回进行中），"
                          "将随下次保存或启动自愈同步。")
            self._set_undo_enabled(bool(self.stack))
        return dropped
