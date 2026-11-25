import os
import time
import json
import shutil
from infra.logutil import _append_utf8_bom
from infra.paths import UNDO_GROUP_FILE, UNDO_INFLIGHT_FILE, UNDO_PENDING_FILE, _undo_base


def _clear_inflight():
    """B1：撤回记录已安全转移（落盘/暂存）后删除 inflight 增量账本；
    不存在或删除失败均忽略（失败则下次启动按残留自愈，内容去重防重复入栈）。"""
    try:
        os.remove(UNDO_INFLIGHT_FILE)
    except OSError:
        pass


# ---------- B2 账本双保险（.bak / .corrupt） ----------
_UNREADABLE = object()  # 哨兵：文件存在但解析失败（区别于文件缺失与 JSON null）


def _try_parse_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return _UNREADABLE


def _backup_undo_json(path):
    """B2 写侧：覆盖写撤回历史前，把现存的旧账本复制为 .bak（只留 1 份）。
    配合写端的 .tmp+os.replace 原子写：原子写防「写一半」，.bak 防「已写坏」。"""
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")


def _read_undo_json_safe(path):
    """B2 读侧：读撤回历史 JSON，返回 (data, issues)。
    data=解析结果（文件缺失或主备双坏为 None）；issues=需呈现给用户的告警列表。
    主文件解析失败先试 .bak（成功则提示已从备份恢复）；主备双坏则把主文件
    改名 .corrupt-<时间戳> 保留现场并醒目告警——绝不静默置空（断电写一半、
    磁盘损坏、手改坏都不得让用户无声失去撤回能力）。"""
    issues = []
    if not os.path.exists(path):
        return None, issues
    data = _try_parse_json(path)
    if data is not _UNREADABLE:
        return data, issues
    bak = _try_parse_json(path + ".bak")
    if bak is not _UNREADABLE:
        issues.append("[撤回] 撤回历史文件损坏，已从 .bak 备份恢复。")
        return bak, issues
    # 主备双坏：隔离主文件保留现场（改名而非删除，留给人工排查/修复）
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        os.replace(path, f"{path}.corrupt-{stamp}")
    except OSError:
        pass
    issues.append("[撤回] 撤回历史文件与备份均已损坏，已将现场保留为 "
                  ".corrupt 文件（撤回能力暂失，请检查磁盘/手动修复）。")
    return None, issues


def _scatter_group_id(parent_dir):
    """确定本次右键散开的分组 id：组文件存在、10 秒内创建、且父目录相同 → 复用其 id
    （视为同一次多选操作）；否则原子创建新组（O_EXCL 先到先得，撞车则重读）。
    分组在进程启动时判定——多选的 N 个进程由 Explorer 同时拉起，必落入同一 10 秒窗口，
    与每个文件夹实际散开耗时无关（锁排队可能让后续进程很久才执行完）。
    返回 (gid, created)：created=True 表示本进程新建组（即一次操作的第一个进程），
    用于「一次操作只弹一次桌面提示」。"""
    gid = f"{os.getpid():x}{int(time.time() * 1000):x}"
    for _ in range(4):
        d = None
        try:
            with open(UNDO_GROUP_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                d = None
        except Exception:
            d = None
        if d is None:
            # 读不出有效 JSON 分两种：超过分组窗口的陈旧残骸（断电/强杀留下）须删除
            # 自愈，否则 O_EXCL 永远 FileExistsError，右键分组永久静默退化；
            # 窗口内的 0 字节文件多半是别的进程刚 O_EXCL 还没写完——不删，留给
            # 下面 O_EXCL 撞 FileExistsError 后循环重读加入该组。
            try:
                if os.path.getmtime(UNDO_GROUP_FILE) < time.time() - 10:
                    os.remove(UNDO_GROUP_FILE)
            except OSError:
                pass  # 不存在（正常）或刚被别人删/建，后续流程自行消化
        if d:
            try:
                fresh = (time.time() - float(d.get("time", 0))) < 10
            except Exception:
                fresh = False
            if fresh and d.get("parent") == parent_dir and d.get("id"):
                return d["id"], False
            # 过期或是另一次操作（父目录不同）：删除后重建
            try:
                os.remove(UNDO_GROUP_FILE)
            except OSError:
                pass
            continue
        try:
            fd = os.open(UNDO_GROUP_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            continue  # 别人抢先创建，循环回去读它的 id
        except OSError:
            break
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"id": gid, "parent": parent_dir, "time": time.time()}, f)
        except Exception:
            pass
        return gid, True
    return gid, True  # 极端竞态兜底：各自成组（退化为每文件夹一批）


def _append_undo_pending(records, group=None):
    """GUI 未开时把右键散开的撤回批次暂存到 pending 文件，待主程序启动合并。
    group 非空时与文件末尾同组条目合并（一次右键多选 = 一批撤回）。"""
    if not records:
        return
    try:
        data = []
        if os.path.exists(UNDO_PENDING_FILE):
            try:
                with open(UNDO_PENDING_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, list):
                    data = []
            except Exception:
                data = []
        if (group and data and isinstance(data[-1], dict)
                and data[-1].get("group") == group
                and isinstance(data[-1].get("records"), list)):
            data[-1]["records"].extend(records)
        elif group:
            data.append({"group": group, "records": list(records)})
        else:
            data.append(records)
        with open(UNDO_PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        # 静默进程无 UI：暂存失败至少写 error.log 留痕，避免撤回批次无声丢失
        try:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            _append_utf8_bom(os.path.join(_undo_base, "error.log"),
                             f"[{stamp}] [错误] 撤回暂存写入失败：{e}\n")
        except Exception:
            pass
