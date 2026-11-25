import os
import sys
import time
import threading
from infra.paths import CRASH_LOG


# GUI 日志框最多保留的行数（QTextDocument 按块计）：超出自动丢弃最旧的，
# 防 10 万文件级散开时日志无限驻留内存、界面越用越卡；首次触顶在日志区
# 提示一次（复制错误/导出从此仅包含最近内容）
LOG_MAX_BLOCKS = 50000


def _append_utf8_bom(path, text):
    """以 UTF-8 追加文本，并确保文件以 UTF-8 BOM 开头（记事本按 UTF-8 解码中文，避免乱码）。
    若文件已存在但缺少 BOM（如旧日志），一次性整体重写补 BOM，避免记事本把开头旧内容识别成 ANSI 乱码。"""
    try:
        if os.path.exists(path) and os.path.getsize(path) >= 3:
            with open(path, "rb") as fb:
                has_bom = fb.read(3) == b"\xef\xbb\xbf"
        else:
            has_bom = False
        if has_bom:
            with open(path, "a", encoding="utf-8") as f:
                f.write(text)
        else:
            # 缺失 BOM：小文件整体重写补 BOM；超大文件（>5MB）不读全量，
            # 直接以 utf-8 追加，避免日志膨胀到 GB 时 read() 整文件进内存导致 OOM
            # 文件首次创建时并不存在，直接 getsize 会抛异常并被外层吞掉，
            # 导致干净目录（例如全新安装的打包版）永远写不出第一份日志。
            size = os.path.getsize(path) if os.path.exists(path) else 0
            if size > 5 * 1024 * 1024:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(text)
            else:
                old = ""
                try:
                    with open(path, "r", encoding="utf-8") as fr:
                        old = fr.read()
                except Exception:
                    old = ""
                with open(path, "w", encoding="utf-8-sig") as fw:
                    fw.write(old + text)
    except Exception:
        pass


def _install_crash_hooks():
    """把主线程/子线程未捕获异常的 traceback 写入 crash.log。
    PyQt6 中槽函数或 QThread.run 抛出的未捕获异常会直接终止进程（闪退），
    pythonw 又无 stderr 可看，必须自行留痕。"""
    import traceback as _tb

    def _dump(kind, text):
        try:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(CRASH_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{stamp}] {kind}\n{text}\n")
        except Exception:
            pass

    def _sys_hook(exc_type, exc_val, exc_tb):
        # SystemExit/KeyboardInterrupt 是正常退出路径（sys.exit(0)、Ctrl+C），
        # 不是闪退——写 crash.log 会让下次启动误报「检测到上次运行异常退出」
        if issubclass(exc_type, (SystemExit, KeyboardInterrupt)):
            sys.__excepthook__(exc_type, exc_val, exc_tb)
            return
        _dump("主线程未捕获异常",
              "".join(_tb.format_exception(exc_type, exc_val, exc_tb)))
        sys.__excepthook__(exc_type, exc_val, exc_tb)

    def _thread_hook(args):
        _dump(f"线程 {args.thread.name if args.thread else ''} 未捕获异常",
              "".join(_tb.format_exception(args.exc_type, args.exc_value,
                                           args.exc_traceback)))

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook


def _trim_log_if_big(path, limit=3 * 1024 * 1024, keep=1024 * 1024):
    """日志超过 limit 时裁剪到只留最后 keep 字节（按整行切），防无限增长。
    裁剪是「保留尾部最新内容」，不影响启动载入失败会话的逻辑。"""
    try:
        if not os.path.exists(path) or os.path.getsize(path) <= limit:
            return
        with open(path, "rb") as f:
            f.seek(-keep, os.SEEK_END)
            f.readline()  # 丢弃可能被截断的首行
            tail = f.read()
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(tail)
        os.replace(tmp, path)
    except Exception:
        pass
