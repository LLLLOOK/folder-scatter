"""B8 移植测试：长路径删除 DeleteFileW 回退。

缺陷复现：\\\\?\\ 扩展前缀路径上 os.remove 在部分环境报 Errno 123，旧版无回退，
.~keep 暂存文件删不掉留残骸。测试用 monkeypatch 让 os.remove 对扩展前缀路径
抛 OSError(123)，模拟该环境；修复后应回退 DeleteFileW 真正删除。
把修复回退（_remove_file 改回 os.remove）时主用例应变红（.~keep 残留）。
"""

import os

import pytest

import core.fileops as fileops
from core.fileops import _fast_move_file, _remove_file


def _make_long_file(tmp_path, name, content):
    """在 tmp_path 下造一个绝对路径 >=260 字符的文件，返回普通形式路径。"""
    deep = tmp_path
    # 逐层加深目录直到路径足够长——按「含文件名的最终路径」判定：
    # 不同机器的 pytest 临时目录长度不同，只比目录部分（旧阈值 240）
    # 会在临时目录较短的机器上造出 <260 的路径，前置断言直接假失败
    while len(str(deep / name)) < 262:
        deep = deep / ("d" * 24)
    deep.mkdir(parents=True, exist_ok=True)
    p = deep / name
    ext = "\\\\?\\" + os.path.abspath(os.path.normpath(str(p)))
    with open(ext, "w", encoding="utf-8") as f:
        f.write(content)
    assert len(os.path.abspath(str(p))) >= 260
    return str(p), deep


@pytest.fixture
def broken_os_remove(monkeypatch):
    """模拟「os.remove 对 \\\\?\\ 扩展前缀路径报 Errno 123」的环境。
    只拦 os.remove（os.unlink 是独立属性，pytest 临时目录清理不受影响）。"""
    real_remove = os.remove

    def fake_remove(path):
        if str(path).startswith("\\\\?\\"):
            raise OSError(123, "文件名、目录名或卷标语法不正确。")
        return real_remove(path)

    monkeypatch.setattr(os, "remove", fake_remove)


def test_remove_file_falls_back_to_deletefilew(tmp_path, broken_os_remove):
    p, deep = _make_long_file(tmp_path, "victim.txt", "x")
    ext = "\\\\?\\" + os.path.abspath(os.path.normpath(p))

    _remove_file(ext)  # os.remove 被模拟为失败 → 必须回退 DeleteFileW 删掉

    leftovers = [f for f in os.listdir("\\\\?\\" + os.path.abspath(str(deep)))
                 if f == "victim.txt"]
    assert leftovers == [], "os.remove 失败时 DeleteFileW 回退须真正删除"


def test_fast_move_overwrite_long_path_leaves_no_keep(tmp_path, broken_os_remove):
    """端到端：长路径覆盖移动后，.~keep 暂存文件不得残留（修复前会残留）。"""
    old_p, deep = _make_long_file(tmp_path, "target.txt", "old")
    new_p, _ = _make_long_file(tmp_path, "source.txt", "new-content")

    _fast_move_file(new_p, old_p)  # to_trash=False：覆盖后物理删除 .~keep

    ext_deep = "\\\\?\\" + os.path.abspath(os.path.normpath(str(deep)))
    names = os.listdir(ext_deep)
    keeps = [n for n in names if ".~keep" in n]
    assert keeps == [], f".~keep 残骸未删除: {keeps}"
    with open("\\\\?\\" + os.path.abspath(os.path.normpath(old_p)),
              "r", encoding="utf-8") as f:
        assert f.read() == "new-content", "新文件必须成功落位"


def test_remove_file_non_prefixed_path_reraises(tmp_path, monkeypatch):
    """普通路径（无扩展前缀）os.remove 失败不得走 DeleteFileW，原样抛出。"""
    def fake_remove(path):
        raise OSError(5, "拒绝访问")

    monkeypatch.setattr(os, "remove", fake_remove)
    p = tmp_path / "normal.txt"
    p.write_text("x", encoding="utf-8")

    with pytest.raises(OSError):
        _remove_file(str(p))


def test_remove_file_normal_path_works_without_fallback(tmp_path):
    p = tmp_path / "ok.txt"
    p.write_text("x", encoding="utf-8")
    _remove_file(str(p))
    assert not p.exists()
