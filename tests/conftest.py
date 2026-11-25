import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# IPC 通道隔离：测试用独立通道名，绝不连到用户正在运行的 GUI。
# 不隔离时 test_a4/test_a2/test_b1 的「静默进程」会连上真 GUI（同机开着软件时），
# 假撤回批次被写进真实 undo_history.json，且用例自身断言失败（已实际发生过）。
os.environ.setdefault("SCATTER_IPC_NAME", f"FolderScatterIPCTest{os.getpid()}")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
