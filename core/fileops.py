import os
import sys
import shutil
import send2trash
from infra.fileutil import _classify_oserror, _clear_readonly, _is_hidden, _lp, _oserr_text, _safe_op, _unique_dst
from infra.paths import BACKUP_DIR_NAME
from core.guards import _is_link_or_junction, _is_readonly
from backup.vault import _backup_overwritten, _restore_backup_to


def _remove_file(path):
    """删除单个文件（B8 移植）：os.remove 失败且路径带 \\\\?\\ 扩展前缀时，
    回退 Win32 DeleteFileW——部分环境 os.remove 对扩展前缀路径报 Errno 123
    「文件名、目录名或卷标语法不正确」，无回退会留 .~keep 残骸。
    仍失败抛 OSError，调用方按既有策略兜底（.~keep 残留，宁留勿丢）。"""
    try:
        os.remove(path)
        return
    except OSError:
        if not str(path).startswith("\\\\?\\") or sys.platform != "win32":
            raise
    import ctypes
    if not ctypes.windll.kernel32.DeleteFileW(str(path)):
        raise OSError(f"删除失败（DeleteFileW 回退亦失败）: {path}")


def _fast_move_file(src, dst, to_trash=False):
    """Windows 上同盘移动文件：覆盖时先删目标再 rename，避免复制大文件。
    to_trash=True 时，覆盖删除旧文件改为送入回收站（可找回），而非物理删除。
    路径超过 260 字符（MAX_PATH）时，对 src/dst 加 Windows 扩展路径前缀（\\\\?\\）绕过限制。"""
    ap_s = os.path.abspath(os.path.normpath(src))
    ap_d = os.path.abspath(os.path.normpath(dst))
    # 源与目标相同（如把根目录散到自身）：直接返回，
    # 否则下方「先删目标再 rename」会把文件自身删掉
    if os.path.normcase(ap_s) == os.path.normcase(ap_d):
        return
    # 任一超长则双双加前缀，确保 os.rename 两端处于同一（扩展）命名空间；
    # UNC（\\server\share）须用 \\?\UNC\ 形式，直接拼 \\?\ 是非法路径
    if len(ap_s) >= 260 or len(ap_d) >= 260:
        def _pre(p):
            if p.startswith("\\\\?\\"):
                return p
            if p.startswith("\\\\"):
                return "\\\\?\\UNC\\" + p.lstrip("\\")
            return "\\\\?\\" + p
        s, d = _pre(ap_s), _pre(ap_d)
    else:
        s, d = src, dst
    try:
        keep = None
        if os.path.exists(d):
            _clear_readonly(d)
            # 旧文件先同盘 rename 到 .~keep 暂存：等新文件成功落位后才处置，
            # 杜绝「旧文件已删/入回收站但新文件没搬成」的静默丢数据
            keep = _unique_dst(d + ".~keep")
            os.rename(d, keep)
        try:
            os.rename(s, d)
        except OSError:
            # 搬移失败：旧文件按策略处置后抛给外层兜底
            if keep:
                try:
                    if to_trash:
                        # 回收站调用只传原始路径：send2trash 的 legacy 后端
                        # （SHFileOperation）靠 8.3 短名转换处理长路径，
                        # 加 \\?\ 前缀反而常被拒绝
                        send2trash.send2trash(keep)
                    elif not os.path.exists(d):
                        os.rename(keep, d)  # 物理覆盖策略：还原旧文件，兜底分支再覆盖
                except OSError:
                    pass  # 处置失败则 .~keep 残留，数据不丢
            raise
        if keep:
            try:
                if to_trash:
                    send2trash.send2trash(keep)
                else:
                    _remove_file(keep)  # B8：长路径 os.remove 失败回退 DeleteFileW
            except OSError:
                pass  # 回收站不可用等：.~keep 残留当保底，宁留勿丢
    except OSError:
        # rename 失败（跨盘/占用）：退化为 shutil.move（复制+删源）完成移动；
        # 仍失败则抛给 move_item 分类
        if to_trash and os.path.exists(d) and (not keep or not os.path.exists(keep)):
            # 覆盖保护开着但旧文件 .~keep 暂存失败（瞬时被占用等；注意 keep 变量
            # 在暂存前已赋值，须以「keep 文件实际不存在」判定暂存未发生）：直接
            # shutil.move 会让 copy2 静默裸覆盖旧文件——先送回收站，
            # 回收站也失败则抛错跳过本次覆盖，绝不静默丢数据
            try:
                send2trash.send2trash(dst)  # 传原始路径：\\?\ 前缀常被回收站拒绝
            except Exception:
                raise
        _safe_op(lambda: shutil.move(s, d))


# 内部 mode 值 → 日志展示文案（内部判定仍用 move/copy，日志面向用户统一中文）
_MODE_CN = {"move": "移动", "copy": "复制"}


def move_item(src, dst, mode, conflict, emit_log, **kw):
    """把单个文件或文件夹按策略搬到 dst（只处理一层）。"""
    try:
        _move_item_impl(src, dst, mode, conflict, emit_log, **kw)
    except (OSError, shutil.Error) as e:
        kind = _classify_oserror(e)
        errfn = kw.get("errfn")
        if errfn is not None:
            errfn(kind)
        if kind == "occupied":
            emit_log(f"[错误] 文件被占用：{src}")
            emit_log("[建议] 请关闭当前占用此文件的软件，然后重新处理此文件夹。")
        elif kind == "permission":
            emit_log(f"[错误] 权限不足：{src}")
            emit_log("[建议] 请检查文件只读属性，或右键软件图标选择“以管理员身份运行”后重试。")
        else:
            emit_log(f"[错误] 无法处理 {src} -> {dst}: {e}")
    except Exception as e:
        # 非 OS 类异常（如 send2trash 内部错误、RecursionError）：
        # 兜底拦截并打日志，绝不让单个文件炸掉整个散开任务（半移动 + 半撤回记录）
        emit_log(f"[错误] 无法处理 {src} -> {dst}: {type(e).__name__}: {e}")


def _move_item_impl(src, dst, mode, conflict, emit_log, **kw):
    """搬运调度：停止检查/隐藏过滤后，目录走 _move_dir、文件走 _move_file。"""
    # 中途停止检查
    stop_check = kw.get("stop_check")
    if stop_check is not None and stop_check():
        return

    undo_records = kw.get("undo_records")

    def _rec_push(rec):
        # B1：每条撤回记录产生时回调 recfn（execute_scatter 挂入，满 20 条增量落盘）
        undo_records.append(rec)
        recfn = kw.get("recfn")
        if recfn is not None:
            recfn()

    # 忽略系统隐藏文件/文件夹（仅当开启 skip_hidden 时）
    if kw.get("skip_hidden", False) and _is_hidden(src):
        return

    if os.path.isdir(src):
        _move_dir(src, dst, mode, conflict, emit_log, kw, _rec_push)
    else:
        _move_file(src, dst, mode, conflict, emit_log, kw, _rec_push)


def _move_dir(src, dst, mode, conflict, emit_log, kw, _rec_push):
    """文件夹分支：目标不存在整体搬；目标是文件报类型冲突；目标是目录递归合并。"""
    stop_check = kw.get("stop_check")
    simulate = kw.get("simulate", False)
    undo_records = kw.get("undo_records")
    # 链接/junction/挂载点：仅允许「目标不存在时的整体移动」（rename 搬的是链接本身）；
    # 复制（copytree 会穿透复制目标内容）与合并（会穿透搬走目标位置的内容）一律跳过
    if _is_link_or_junction(src) and (mode != "move" or os.path.exists(dst)):
        emit_log(f"[跳过] 链接或挂载点（避免穿透到其目标位置）: {src}")
        return
    if not os.path.exists(dst):
        if simulate:
            emit_log(f"[预览] 将处理文件夹: {src} -> {dst}")
        else:
            if mode == "move":
                try:
                    os.rename(_lp(src), _lp(dst))
                except OSError:
                    # 跨盘（挂载点/subst 盘）os.rename 报 WinError 17：退化为 shutil.move
                    _safe_op(lambda: shutil.move(_lp(src), _lp(dst)))
            else:
                shutil.copytree(_lp(src), _lp(dst))
            emit_log(f"[{_MODE_CN[mode]}] {src} -> {dst}")
            if undo_records is not None:
                # copy 模式（整目录复制）同样记录：撤回时把复制出来的整个文件夹送
                # 回收站，与单文件 copy 的撤回语义一致；否则复制散出的整棵子文件
                # 夹在撤回后会原样残留
                _rec_push({"src": src, "dst": dst, "mode": mode})
    else:
        # 类型冲突：目标是【文件】而源是【文件夹】，无法合并——
        # 明确报一条错返回，避免逐个搬子项（父路径是文件必败）刷 N 行错误日志
        if os.path.isfile(dst):
            emit_log(f"[错误] 类型冲突：目标是文件而源是文件夹，无法合并: {src} -> {dst}")
            errfn = kw.get("errfn")
            if errfn is not None:
                errfn("other")
            return
        # 目标已存在同名文件夹 -> 递归合并
        try:
            names = os.listdir(src)
        except OSError as e:
            emit_log(f"[错误] 无法读取子文件夹 {src}: {_oserr_text(e)}")
            return
        for name in names:
            if name == BACKUP_DIR_NAME:
                continue  # 合并路径同样不搬备份目录
            if stop_check is not None and stop_check():
                return
            s = os.path.join(src, name)
            d = os.path.join(dst, name)
            move_item(s, d, mode, conflict, emit_log, **kw)
        if mode == "move" and not simulate:
            try:
                if not os.listdir(src):
                    os.rmdir(src)
            except OSError:
                pass


def _file_skipped_by_filters(src, emit_log, kw):
    """文件级过滤：后缀黑白名单（仅对文件生效）+ 大文件保护。命中跳过返回 True。"""
    suffixes = kw.get("suffixes")          # None = 全部文件
    suffix_exclude = kw.get("suffix_exclude", False)  # False=只散开(白名单) True=只排除(黑名单)
    max_size_mb = kw.get("max_size_mb", 0)  # 0 = 不限制
    if suffixes is not None:
        ext = os.path.splitext(src)[1].lower().lstrip(".")
        if suffix_exclude:
            if ext in suffixes:
                return True  # 只排除：命中后缀，静默跳过
        elif ext not in suffixes:
            return True  # 只散开：不符合后缀，静默跳过（不计入移动）
    # 大文件保护
    if max_size_mb and max_size_mb > 0:
        try:
            sz = os.path.getsize(src)
        except OSError:
            sz = 0
        if sz > max_size_mb * 1024 * 1024:
            emit_log(f"[跳过] 文件过大 ({sz/1024/1024:.1f}MB > {max_size_mb}MB): "
                     f"{os.path.basename(src)}")
            skipfn = kw.get("skipfn")
            if skipfn is not None:
                skipfn()
            return True
    return False


def _backup_for_overwrite(dst, mode, tag, to_trash, emit_log, kw):
    """覆盖前保护（to_trash 开且确为覆盖时）：旧文件先移入备份批次目录（撤回可还原）。
    返回 (backup, proceed)：proceed=False 表示保护失败，必须跳过本次覆盖。
    move 备份失败交给 _fast_move_file 内部回收站兜底；copy 必须先自行处置 dst，
    否则 copy2 会静默裸覆盖。"""
    if tag != "覆盖" or not to_trash:
        return "", True
    # 旧文件先移入同盘备份根目录的批次子目录（撤回可自动还原）；
    # 备份失败时 dst 仍在，下方落位会回退送回收站
    backup = _backup_overwritten(dst, kw.get("backup_batch", ""),
                                 scatter_parent=kw.get("backup_parent", ""))
    if backup:
        emit_log(f"[备份] 被覆盖的旧文件已移入备份: {backup}"
                 f"（撤回时自动还原）")
        return backup, True
    if mode == "move":
        return "", True
    # copy 备份失败兜底：旧文件送回收站；回收站也失败则跳过
    # 本次覆盖，绝不静默丢数据
    try:
        send2trash.send2trash(dst)
    except OSError:
        emit_log(f"[错误] 旧文件保护失败（备份与回收站均不可用），"
                 f"跳过本次覆盖: {dst}")
        errfn = kw.get("errfn")
        if errfn is not None:
            errfn("other")
        return "", False
    return "", True


def _restore_backup_on_failure(backup, dst, emit_log):
    """B3：走备份的覆盖，落位失败必须把备份还原回 dst 原位——
    否则旧文件留在备份目录成孤儿、原位空了、撤回记录也没生成。"""
    if not backup:
        return
    if _restore_backup_to(backup, dst):
        emit_log(f"[备份] 落位失败，备份已还原回原位: {dst}")
    else:
        emit_log(f"[错误] 落位失败且备份还原失败，"
                 f"备份保留在: {backup}")


def _move_file(src, dst, mode, conflict, emit_log, kw, _rec_push):
    """文件分支：过滤 → 模拟 → 冲突策略（跳过/重命名/覆盖+备份）→ 落位 → 记撤回。"""
    simulate = kw.get("simulate", False)
    countfn = kw.get("countfn")
    to_trash = kw.get("to_trash", False)   # 覆盖保护开关：覆盖前旧文件移入备份（失败兜底回收站），move/copy 的覆盖均生效
    undo_records = kw.get("undo_records")

    if _file_skipped_by_filters(src, emit_log, kw):
        return

    if simulate:
        emit_log(f"[预览] 将移动: {src} -> {dst}")
        if countfn is not None:
            countfn()
        return

    if os.path.exists(dst):
        if conflict == "skip":
            emit_log(f"[跳过] 已存在: {dst}")
            return
        tag = "覆盖"
        # 文件撞【同名目录】时无法覆盖（os.remove 对目录失败、shutil.move 会把文件
        # 移进目录里），与冲突策略无关，一律改走自动重命名
        if conflict == "rename" or os.path.isdir(dst):
            dst = _unique_dst(dst)
            tag = "重命名"
        # 只读目标视为用户刻意的防删标记：散开路径跳过覆盖并报错，
        # 不静默清属性后删除/覆盖（撤回还原路径仍保留清属性能力）
        if tag == "覆盖" and _is_readonly(dst):
            emit_log(f"[警告] 目标文件为只读，跳过本次覆盖以免删除您保护的文件: {dst}")
            errfn = kw.get("errfn")
            if errfn is not None:
                errfn("permission")
            return
        backup, proceed = _backup_for_overwrite(dst, mode, tag, to_trash,
                                                emit_log, kw)
        if not proceed:
            return
        try:
            if mode == "move":
                _fast_move_file(src, dst, to_trash=to_trash)
            else:
                _safe_op(lambda: shutil.copy2(_lp(src), _lp(dst)))
        except Exception:
            _restore_backup_on_failure(backup, dst, emit_log)
            raise
        if undo_records is not None:
            rec = {"src": src, "dst": dst, "mode": mode}
            if tag == "覆盖":
                if backup:
                    rec["backup"] = backup
                else:
                    # 无备份（失败或未开保护）：旧文件入回收站或被物理删除，
                    # 撤回无法自动还原，据此提示用户手动找回
                    rec["overwrote"] = True
            _rec_push(rec)
        emit_log(f"[{_MODE_CN[mode]}·{tag}] {src} -> {dst}")
    else:
        if mode == "move":
            _fast_move_file(src, dst, to_trash=to_trash)
        else:
            _safe_op(lambda: shutil.copy2(_lp(src), _lp(dst)))
        if undo_records is not None:
            _rec_push({"src": src, "dst": dst, "mode": mode})
        emit_log(f"[{_MODE_CN[mode]}] {src} -> {dst}")
    if countfn is not None:
        countfn()


def promote_subfolder(src, parent, mode, conflict, emit_log, **kw):
    """把子文件夹 src 的直接内容（不递归更深）散到上一级 parent。"""
    # 链接/junction/挂载点本身不散开（listdir 会穿透到目标位置，把别的目录的内容搬走）
    if _is_link_or_junction(src):
        emit_log(f"[跳过] 链接或挂载点（避免穿透到其目标位置）: {src}")
        return
    try:
        names = os.listdir(src)
    except OSError as e:
        kind = _classify_oserror(e)
        errfn = kw.get("errfn")
        if errfn is not None:
            errfn(kind)
        emit_log(f"[错误] 无法读取子文件夹 {src}: {_oserr_text(e)}")
        return
    for name in names:
        if name == BACKUP_DIR_NAME:
            continue  # 备份目录不参与散开（里面是覆盖旧文件的保护副本）
        s = os.path.join(src, name)
        d = os.path.join(parent, name)
        move_item(s, d, mode, conflict, emit_log, **kw)
    if mode == "move" and not kw.get("simulate", False):
        try:
            if not os.listdir(src):
                os.rmdir(src)
        except OSError:
            pass


def count_files(path, recursive, stop=None, depth=0, size_acc=None):
    """估算文件夹内文件数量（用于进度条上限）。
    depth 上限与 build_tree_item 一致（防异常深嵌套递归爆栈，超限部分少计、
    进度上限偏低无害，结束时进度条会 snap 到满）；
    stop 回调用于计数阶段响应「停止」（超大目录计数不再无响应）；
    size_acc: 可选列表，传入时把每个文件的字节数一并收集（copy 模式磁盘空间预检用）。"""
    MAX_DEPTH = 100
    total = 0
    try:
        for n in os.listdir(path):
            if stop is not None and stop():
                return total
            p = os.path.join(path, n)
            if os.path.isdir(p):
                # 不穿透链接/junction（与散开行为一致，也防循环链接递归爆栈）
                if recursive and not _is_link_or_junction(p) and depth < MAX_DEPTH:
                    total += count_files(p, recursive, stop=stop, depth=depth + 1,
                                         size_acc=size_acc)
            else:
                total += 1
                if size_acc is not None:
                    try:
                        size_acc.append(os.path.getsize(p))
                    except OSError:
                        pass
    except OSError:
        pass
    return total
