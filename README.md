<div align="center">
  <img src="assets/icons/appicon_128.png" width="96" alt="文件夹散开助手图标">

  # 文件夹散开助手

  **把嵌套文件夹一键摊平，散错了还能原样撤回。**

  Windows 文件夹整理工具 · 模拟运行 · 覆盖备份 · 按批撤回 · 资源管理器右键菜单

  Flatten nested folders on Windows with dry-run, conflict handling, backup and batch undo.

  **[下载 Windows 免安装版](https://github.com/LLLLOOK/folder-scatter/releases/download/v1.0.0/FolderScatter-v1.0.0.exe)** ·
  [图文介绍与国内网盘](https://folder-scatter.app.workbuddy.host/?sharecode=YRIDAC7VQN) ·
  [使用说明](#使用方法) ·
  [问题反馈](https://github.com/LLLLOOK/folder-scatter/issues/new/choose)

  [![Latest release](https://img.shields.io/github/v/release/LLLLOOK/folder-scatter?label=release)](https://github.com/LLLLOOK/folder-scatter/releases/latest)
  ![Windows 10/11](https://img.shields.io/badge/Windows-10%20%2F%2011-0078D4)
  ![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB)
  [![GPL-3.0 license](https://img.shields.io/badge/license-GPL--3.0-blue)](LICENSE)

  <img src="docs/screenshots/main.png" width="1000" alt="文件夹散开助手主界面">
</div>

## 为什么需要它

压缩包、下载资料和客户素材解压后，经常出现「文件夹套文件夹套文件夹」。手动剪切、返回、粘贴不仅慢，还容易漏文件。

文件夹散开助手可以把选中目录里的内容批量上提或彻底摊平；操作前能先模拟，操作后能按批撤回，不用一层层手工搬运。

## 核心功能

| 功能 | 你能用它做什么 |
|---|---|
| **三档散开深度** | 只处理当前层、包含一层子文件夹，或把全部层级彻底摊平 |
| **按批撤回** | 每次操作单独记账，关闭软件后也能撤回最近 20 批；撤回时恢复目录树勾选状态 |
| **资源管理器右键菜单** | 不必先打开主程序，右键文件夹即可散开；日志会同步到已打开的窗口 |
| **模拟运行** | 先查看处理清单，不改动磁盘，确认无误后再正式执行 |
| **冲突处理与备份** | 同名文件可覆盖、跳过或自动重命名；覆盖前可备份，撤回时连旧文件一起还原 |
| **移动、复制与筛选** | 支持移动或复制，按后缀筛选、忽略大文件、跳过隐藏与系统文件 |

另外提供深色 / 浅色 / 跟随系统主题、彩色日志与错误导出、后台扫描、任务栏进度、系统托盘和开机自启。

## 下载与安装

普通用户直接下载 **[FolderScatter-v1.0.0.exe](https://github.com/LLLLOOK/folder-scatter/releases/download/v1.0.0/FolderScatter-v1.0.0.exe)**：

- 适用于 **Windows 10 / 11 64 位**，无需安装 Python，双击即用。
- 单文件免安装，约 20 MB；所有版本与更新说明见 [Releases](https://github.com/LLLLOOK/folder-scatter/releases)。
- 首次运行若出现 SmartScreen 提示，可点击「更多信息」→「仍要运行」。请只从本仓库或上方介绍页下载。
- 软件完全离线，不上传文件；运行数据保存在 `%APPDATA%\文件夹散开助手\`。

## 使用方法

1. 把要处理的文件夹拖进窗口，或点击「浏览」选择它的**上一级目录**。
2. 在目录树中勾选要散开的文件夹；按住 `Shift` 可以连续选择。
3. 选择散开深度、移动或复制，以及同名文件的处理方式。
4. 先点「模拟运行」核对清单，再点「开始散开」。
5. 如果结果不符合预期，点击「撤回」即可按批还原。

### 从资源管理器直接使用

打开「设置」，勾选需要的右键菜单项。之后右键文件夹，就能选择：

- 仅当前层散开
- 包含子文件夹散开
- 全部层级散开
- 在软件中打开（可撤回）

## 文件安全设计

- **危险位置拦截**：拒绝系统目录、用户目录根和磁盘根目录；不会顺着 junction / 符号链接进入其他目录。
- **覆盖先备份**：旧文件先进入同盘备份目录，新文件落位后才处理旧文件；只读目标跳过覆盖。
- **失败不裸删**：保护步骤失败时回退到回收站，不静默物理删除。
- **异常也有退路**：跨进程互斥避免同时搬运；撤回记录经过可信校验，异常中断后可根据落盘记录继续撤回。

## 更多截图

| 资源管理器右键菜单 | 设置窗口 |
|---|---|
| ![资源管理器右键菜单](docs/screenshots/context-menu.png) | ![设置窗口](docs/screenshots/settings.png) |

## 从源码运行

需要 Windows 10/11 和 Python 3.9+：

```bat
git clone https://github.com/LLLLOOK/folder-scatter.git
cd folder-scatter
pip install -r requirements.txt
python main.py
```

也可以双击 `运行文件夹散开工具.bat`，使用 `pythonw` 启动且不显示控制台。

### 打包成单文件 EXE

```bat
pip install nuitka
打包_Nuitka.bat
```

产物位于 `dist_nuitka_plain_onefile\FolderScatter_plain.exe`。

## 项目结构

分层为 `infra → core / backup / win32 → undo → ui → main`，无循环依赖。

```text
main.py               程序入口与右键静默流程
infra/                路径、文件工具、日志与崩溃钩子
core/                 散开调度、文件搬运、安全防线与进程互斥
backup/               覆盖备份、可信校验与备份维护
undo/                 撤回栈、撤回执行与记录持久化
win32/                资源管理器菜单、开机自启与任务栏进度
ui/                   主窗口、设置、日志、目录树与后台线程
assets/icons/         软件图标
docs/screenshots/     界面与功能截图
docs/development/     重构过程、模块约定与网页素材
docs/testing/         人工验收清单
tests/                自动化回归测试
```

开发者说明见 [agents.md](agents.md)，重构与修复记录见 [docs/development](docs/development/)，人工验收清单位于 [docs/testing](docs/testing/)。

<details>
<summary><strong>数据保存位置与卸载</strong></summary>

| 内容 | 位置 |
|---|---|
| 撤回历史（开发态） | 项目根目录 `undo_history.json` 及相关暂存、备份文件 |
| 撤回历史（打包态） | `%APPDATA%\文件夹散开助手\` |
| 覆盖备份 | `<盘>:\散开备份\<批次>\`；盘根不可写时回退到 `.scatter_backup` |
| 崩溃日志 | `crash.log`，与撤回历史位于同一目录 |
| 软件设置 | 注册表 `HKCU\Software\FolderPromoter\FolderPromoter` |
| 右键菜单 | 注册表 `HKCU\Software\Classes\Directory\shell\FolderScatter*` |
| 开机自启 | 注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` |

卸载时删除程序及以上由软件创建的数据；软件不写系统目录、不联网。

</details>

## 自动化测试

```bat
python -m pytest tests\ -q
```

测试覆盖长路径、同名冲突、覆盖备份与还原、撤回栈、IPC 幂等和备份清理联动等场景。

## 已知限制

- 仅支持 Windows。
- 右键多选时，同一次操作是否合并为一批撤回依赖 10 秒时间窗。
- 8.3 短文件名场景下，个别同文件判断可能不准确。
- 不建议多个窗口同时写同一份撤回历史。
- 暂不支持云同步和多语言界面。

## 参与贡献

欢迎通过 [Issue](https://github.com/LLLLOOK/folder-scatter/issues/new/choose) 反馈问题或提出建议。提交代码前请运行自动化测试，并保持文件安全底线：保护步骤失败时必须回退到回收站，不允许静默裸覆盖或物理删除。

## 许可证

本项目基于 [GNU General Public License v3.0](LICENSE) 发布。可自由使用、修改和分发；分发衍生作品时需遵守 GPL-3.0，并保留许可证与版权声明。
