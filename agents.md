# 文件夹散开助手（重构版，多模块）

PyQt6 桌面工具：把文件夹内容上提一层（"散开"）、按批撤回、覆盖备份保护。
右键菜单起静默进程（--silent-folder），经本地 IPC 与 GUI 同步；撤回记录持久化在
undo_history.json（+ pending 暂存），跨会话可撤。

## 怎么跑
- GUI：`运行文件夹散开工具.bat`（pythonw 无控制台）或 `python main.py`
- 静默：`pythonw main.py --silent-folder <目录> [--level one|two|all]`
- 编译验证：`python -m py_compile main.py`
- 自动化回归（改代码后必跑）：同一 python `-m pytest tests\ -v`

## 模块地图（改哪里先找这里）
分层 `infra → core/backup/win32 → undo → ui → main`，无循环依赖。
| 模块 | 职责 |
|---|---|
| `main.py` | 入口：命令行解析、`run_silent_folder` 右键静默全流程、GUI 启动 |
| `core/scatter.py` | 散开统一调度（GUI/静默共用）：`_expand_selected_by_depth`、`execute_scatter` |
| `core/fileops.py` | 单文件搬运原语：`move_item`、冲突策略、备份联动、`_fast_move_file` |
| `core/guards.py` | 安全防线：链接/junction 识别、系统目录保护集、只读判定 |
| `core/locking.py` | 跨进程互斥（命名互斥体）：散开锁 + 历史写锁 `_history_lock` |
| `backup/vault.py` | 备份写入/还原原语、批次目录、根枚举、批次清理 |
| `backup/trust.py` | `_is_undo_path_allowed`、`_is_trusted_backup`、自定义根读取 |
| `backup/maintenance.py` | 备份维护纯函数（无 Qt 可单测）：按天保留、孤儿清理、清空 |
| `undo/history.py` | `UndoHistoryStore`：撤回栈唯一拥有者（纯 Python 无 Qt） |
| `undo/restore.py` | `UndoWorker(QThread)` 撤回执行 |
| `undo/store.py` | 撤回文件层：inflight 清账、.bak/.corrupt 双保险读写、分组 id、pending |
| `win32/menu.py` | 右键菜单/开机自启注册表直写（`MENU_REG_VERSION`） |
| `win32/taskbar.py` | ITaskbarList3 任务栏进度（ctypes，静默降级） |
| `ui/main_window.py` | 主窗口 `App`：装配根（346 行，构造 + 直通属性 + 薄壳转发），业务方法已按面板逐字拆为 mixin，经多继承解析（`main_window.py.bak` 为拆分前留底） |
| `ui/app_build.py` | `BuildMixin`：6 个 `_build_*` 界面构建 + `DropLineEdit`（519 行） |
| `ui/app_run.py` | `RunMixin`：散开执行（开始/模拟/停止）与撤回（348 行） |
| `ui/app_options.py` | `OptionsMixin`：选项回调/记忆设置/后缀解析/设置对话框应用（295 行） |
| `ui/app_lifecycle.py` | `LifecycleMixin`：窗口几何记忆/系统托盘/退出流程（188 行） |
| `ui/app_tree.py` | `TreeOpsMixin`：目录树勾选/清空/增量刷新/浏览（172 行） |
| `ui/app_theme.py` | `ThemeMixin`：主题解析/切换、`_spin_style`/`_tree_style`（112 行） |
| `ui/app_events.py` | `EventsMixin`：悬停状态栏/全局拖拽/resize/mousePress（109 行） |
| `ui/settings_dialog.py` | 设置对话框六大板块 |
| `ui/log_panel.py` | 彩色日志 `LogPanel`、历史/崩溃日志、[备份] 提示联动 |
| `ui/ipc_server.py` | `IpcServer`：QLocalServer、done 防抖、A4 ack/幂等 |
| `ui/scan_controller.py` | 目录树异步扫描调度、退役线程保活（`_retired_scans`）、代际作废 |
| `ui/workers.py` | `Worker` 散开线程、`TreeScanWorker` 建树线程 |
| `ui/widgets.py` | `CheckTreeWidget`（Shift 区间勾选）、建树原语 |
| `ui/style.py` `ui/toast.py` `ui/icons.py` `ui/taskbar_ctl.py` | 主题 QSS / Toast / emoji 图标 / 任务栏进度控制 |
| `infra/paths.py` `infra/fileutil.py` `infra/logutil.py` | 路径常量 / 文件工具（`_lp` 长路径等）/ 日志与 crash hook（`LOG_MAX_BLOCKS=50000`） |
| `tests/` | 11 个 pytest 脚本，与缺陷编号一一对应（A2/A3/A4/B1-B4/B8/F3/store） |

## 约定（不看到就会犯错）
- pythonw 无控制台：未捕获异常 = 静默闪退无痕。excepthook 写 crash.log、启动时日志区
  展示；诊断一律进 UI，不依赖控制台。槽函数/QThread.run 必须整体兜底。
- 运行中的 QThread 必须一直有 Python 引用：引用归零 sip 立即销毁仍在跑的 C++ 线程
  → 0xc0000005 闪退（无痕，WER 里 python313.dll）。refresh() 换扫描线程时旧的进
  `_retired_scans`（ui/scan_controller.py）保活、finished 再摘；退出时一并等停。
- 该坑对**所有 sip /Transfer/ 所有权对象**泛化（QLocalSocket 等）：不能只靠「信号连接
  闭包」循环引用保活——cyclic GC 会整体回收并析构 C++ 对象，之后 Qt 事件打到已删对象
  → c0000005。必须放容器（如 ui/ipc_server.py 的 `_ipc_sockets` set）持有，断开才摘除。
  排查 pythonw 闪退：WER ReportArchive 按时间找报告 → 异常码/故障模块/偏移 → PE 符号
  解析定位（0xc0000005 + python313.dll ≈ 悬空 C++ 对象访问）。
- 文件安全：覆盖时旧文件先同盘暂存（.~keep/备份目录），新文件落位后才处置；任何保护
  失败回退回收站，禁止静默裸覆盖/物理删除。只读目标跳过覆盖。
- 备份布局 `<盘>:\散开备份\<批次>\`（可见；盘根不可写回退父目录 .scatter_backup）。
  保留期 = 撤回记录生命周期：出栈/20 批截断/撤回后自动清理；设置里按天清=移回收站。
- 撤回记录来自 JSON/IPC 不可全信：backup/trust.py 两道校验。
- 清空/删除备份必须三联动：引用该备份的撤回批次整批出栈（UndoHistoryStore.
  invalidate_backups）+ 主日志 [备份] 块同步（LogPanel._sync_backup_notice_log）+
  设置行移除；只删文件不动栈会出「没备份了还能撤回」。纯移动批次无 backup 字段，不受
  备份清理影响。兜底：启动 load_on_startup 再自愈一遍——批次的备份文件已不存在即整批
  出栈并打 [撤回] 日志。
- 保护目录：系统目录前缀 + realpath + subst 解析 + USERPROFILE 根精确匹配；盘根拒绝。
  根自身是 junction/symlink 时 two/all 展开会穿透到链接目标的真实目录——GUI
  （_expand_selected_by_depth 展开前跳过）与右键（run_silent_folder 降级 level=one）
  两入口都在展开前拦，靠 promote 入口既有拦截打跳过日志。
- 后缀过滤的解析（_parse_suffixes）与校验（_validate_custom_suffix）分割正则必须一致
  （中英文逗号+空白）：不一致会出现「校验通过但解析成整坨后缀」的静默失效。
- 日志滚动统一「贴底才跟随」（LogPanel.log_msg / apply_theme 重灌 / resizeEvent 三处
  同策略，容差 maximum-value<=4）；触顶 LOG_MAX_BLOCKS 首次打一条 [警告]
  （清空日志重置标记、主题重灌不重置）。
- UI：文案短白话；设置界面无悬浮提示；保存不弹窗走状态栏；状态栏悬停截断 60 字；
  次级选项缩进、警告行行高常留。彩色 emoji 不直接写进按钮文字（后备字体行高偏大，
  并排按钮会一高一低）——用 setIcon(_emoji_icon(...))（ui/icons.py）。模态对话框里的
  反馈必须放在对话框内（如按钮行内嵌提示）：写主窗口状态栏会被对话框挡住，等于没反馈。
  字形/图标居中不信 QFontMetrics（度量与回退字形实际渲染不一致，实测偏 2-3px）——
  文本字形要真居中用两遍法：先渲染量像素墨迹中心，再平移绘制原点；纯几何图形
  （圆/圆弧）以画布中心直接画即可。冷僻字形（如 ↺ U+21BA）系统字体没有 → drawText
  渲染成豆腐块；这类图标用 QPainter 手绘（先 arcMoveTo 再 arcTo，否则空路径 arcTo
  会从 (0,0) 连线）。图标大小对齐看「墨迹」不看画布。
- 右键菜单文案/命令一旦改动必须递增 win32/menu.py 的 MENU_REG_VERSION：注册表只在
  「勾选变化或版本号变化」时重写，不递增老用户的菜单名不会刷新。

## 现状与下一步
- 测试：tests/ 下 11 个 pytest 脚本与缺陷编号一一对应，回退修复即变红；
  验证全量人工操作用《功能清单与验证方案.html》（72 例）。
- 设置键：confirm_scatter（散开确认「不再提示」）、filter_exclude（只散开/只排除
  持久化）、menu_reg_version（菜单注册版本）。右键菜单项「撤回」现名
  「在软件中打开（可撤回）」。「[撤回] 已将副本送入回收站」日志走橙色。
- 默认主题为深色：所有 theme_mode 读取兜底统一 dark。验证用临时 ini 沙盒 QSettings，
  别写 QSettings("FolderPromoter",...) 真注册表，会污染用户设置。
- 无「清空目录后自动刷新目录树」设置（refresh_after_clear 已废弃）：清空目录统一为
  全部清空（路径+树），启动时清掉残留键。
- ignore_checked/ignore_size 已解耦 remember_suffix：GUI 变更即落盘、启动恒恢复，
  右键静默共用（同 conflict_trash）。
- 已完成优化（2026-10-08）：三个超长函数拆解（settings_dialog __init__ /
  run_silent_folder / _move_item_impl）、main_window.py 按面板拆为 7 个 mixin。
- 遗留优化（低档）：三处小重复（长路径前缀/拖拽拦截/日志攒批/根目录判定）。
- 已知未修（低档，触发条件苛刻）：组 id 10 秒窗口、8.3 短名同文件判定、多开共写历史、
  互斥体可被任意进程抢占、IPC 无 ack 降级。
