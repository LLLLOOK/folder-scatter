import os
import json
from infra.paths import BACKUP_DIR_NAME, UNDO_INFLIGHT_FILE
from core.fileops import promote_subfolder
from core.guards import _is_link_or_junction
from core.locking import _ScatterBusyError, _acquire_scatter_lock, _release_scatter_lock


def _expand_selected_by_depth(selected, level):
    """按散开深度把用户勾选的文件夹扩展成实际要散开的源列表（仿 run_silent_folder）：

      one  仅勾选的文件夹本身；
      two  勾选文件夹 + 其直接子文件夹；
      all  勾选文件夹 + 其下所有层级子文件夹。

    execute_scatter 会再按深度倒序逐个上提一层，实现对应深度的展平。
    散开深度不改动目录树勾选，展开在后台完成；结果去重，避免嵌套勾选导致重复处理。"""
    if level == "one":
        return list(selected)
    seen = set()
    result = []

    def add(p):
        np = os.path.normpath(p)
        if np not in seen:
            seen.add(np)
            result.append(p)

    for folder in selected:
        add(folder)
        # 勾选的文件夹本身是链接/junction：不向下展开（listdir/os.walk 会穿透到
        # 链接目标的真实目录搬走别人的内容），只保留本体——promote 入口的
        # 既有拦截会跳过它并留日志
        if _is_link_or_junction(folder):
            continue
        if level == "two":
            try:
                for d in os.listdir(folder):
                    if d == BACKUP_DIR_NAME:
                        continue  # 备份目录不参与散开
                    sub = os.path.join(folder, d)
                    if os.path.isdir(sub) and not _is_link_or_junction(sub):
                        add(sub)
            except OSError:
                pass
        else:  # all
            for root, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs
                           if d != BACKUP_DIR_NAME
                           and not _is_link_or_junction(os.path.join(root, d))]
                for d in dirs:
                    add(os.path.join(root, d))
    return result


def execute_scatter(selected, parent_dir, mode, conflict,
                    suffixes, max_size_mb, simulate,
                    emit_log, emit_progress, emit_skip, stop_check, undo_records,
                    skip_hidden=False, to_trash=False, suffix_exclude=False,
                    lock_wait_ms=0, backup_batch=""):
    """统一调度散开执行（GUI 线程 / 静默模式共用）。

    注意：散开层数（one/two/all）由调用方在外部决定——静默模式由
    run_silent_folder 按 level 组装 selected，GUI 模式由 apply_default_checks
    决定勾选集合；本函数只按传入的 selected 逐个散开，不接收 level。
    """
    selected = sorted(selected, key=lambda p: p.count(os.sep), reverse=True)
    moved = 0
    folders = 0
    skipped = 0       # 大文件跳过（专用于 skip_label / 总结统计）
    occupied = 0      # 文件被占用次数
    permission = 0    # 权限不足次数

    def countfn():
        nonlocal moved
        moved += 1
        emit_progress(moved)

    def skipfn():
        nonlocal skipped
        skipped += 1
        emit_skip(skipped, occupied, permission)

    def errfn(kind):
        nonlocal occupied, permission
        if kind == "occupied":
            occupied += 1
        elif kind == "permission":
            permission += 1
        emit_skip(skipped, occupied, permission)

    # B1 增量账本：真实散开时把已产出撤回记录增量落盘 inflight 文件——
    # 进程崩溃/断电时本批已移动的文件不至于「一个字都没有」，下次启动
    # 自愈合成批次入栈。每 20 条记录（recfn 逐条回调计数）+ 每个源文件夹
    # 边界 + 收尾各写一次；原子写（.tmp+replace），失败不阻断散开
    _since_flush = 0

    def _flush_inflight(force=False):
        nonlocal _since_flush
        if simulate or undo_records is None:
            return
        _since_flush += 1
        if not force and _since_flush < 20:
            return
        _since_flush = 0
        try:
            _tmp = UNDO_INFLIGHT_FILE + ".tmp"
            with open(_tmp, "w", encoding="utf-8") as f:
                json.dump(undo_records, f, ensure_ascii=False, indent=2)
            os.replace(_tmp, UNDO_INFLIGHT_FILE)
        except OSError:
            pass  # 增量落盘失败不阻断散开（下次 flush 再试）

    def recfn():
        # 每产生一条撤回记录回调一次（fileops 各 append 点挂入）
        _flush_inflight()

    # 模拟运行（预览）只读不写，不抢互斥锁，避免阻塞/被阻塞真实散开
    _lock_held = False
    if not simulate:
        _lock_ok, _lock_reason = _acquire_scatter_lock(lock_wait_ms)
        if not _lock_ok:
            emit_log(f"[错误] {_lock_reason}")
            raise _ScatterBusyError(_lock_reason)
        _lock_held = True
    try:
        for path in selected:
            if stop_check is not None and stop_check():
                emit_log("[停止] 用户已中止操作。")
                break
            promote_subfolder(path, parent_dir, mode, conflict, emit_log,
                              suffixes=suffixes, max_size_mb=max_size_mb,
                              simulate=simulate, stop_check=stop_check,
                              undo_records=undo_records, skipfn=skipfn, countfn=countfn,
                              skip_hidden=skip_hidden, errfn=errfn, to_trash=to_trash,
                              suffix_exclude=suffix_exclude, backup_batch=backup_batch,
                              backup_parent=parent_dir, recfn=recfn)
            folders += 1
            _flush_inflight(force=True)  # 源文件夹边界：每处理完一个源强制落盘
            emit_log(f"[完成] {path}")
        _flush_inflight(force=True)  # 收尾（含中途停止）：在途记录全部落盘

        if simulate:
            emit_log(f"[预览] 扫描完成：本次将移动约 {moved} 个文件。")
        else:
            parts = [f"[总结] 本次共散开 {moved} 个文件，处理 {folders} 个文件夹。"]
            extra = []
            if skipped > 0:
                extra.append(f"跳过 {skipped} 个大文件")
            if occupied > 0:
                extra.append(f"遇到 {occupied} 个被占用文件")
            if permission > 0:
                extra.append(f"{permission} 个权限不足")
            if extra:
                parts.append("，".join(extra) + "，请检查上方红色/黄色日志。")
            emit_log("".join(parts))
    finally:
        if _lock_held:
            _release_scatter_lock()
    return moved, folders
