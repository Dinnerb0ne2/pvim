# PVIM - v0.7

PVIM 是一个基于 **Python 3.14.3**、纯标准库实现的终端编辑器。

## 核心能力

- asyncio 异步调度与外部进程管道（不阻塞渲染）
- 跨平台终端输入层（Windows / Linux / macOS）
- 预测性渲染（输入优先，后台状态低优先级回填）
- Layout Manager + Feature Registry（Tabline / Winbar / Statusline 动态布局）
- 文件树、Tab 补全、Git 控制可插拔模块
- 可选 LSP 客户端（定义/引用/实现/符号/重命名/格式化/代码动作）
- 工作区根目录自动检测（.git / pyproject.toml / package.json）
- 全局与项目级搜索替换（findre/replacere/replaceallre/replaceproj/replaceprojre）
- `/` 增量搜索预览 + 搜索历史（Up/Down）+ `n` 快速重复
- 分屏窗口管理（split/vsplit/wincmd）
- 内置终端面板与终端管理命令（:term open/close/status/history/send）
- 多编码读取与按编码保存（状态栏显示编码）
- Swap 恢复 + 会话档案 + 自动保存（默认不开启启动自动恢复）
- 子进程刷新版本号丢弃机制（只接收最新结果）
- 虚拟文本叠加层与浮动窗口
- PieceTable 底层结构（大文件编辑基础）
- 自定义脚本语言 + 插件管理（安装/卸载/版本/简介）
- 主题管理（安装/卸载/列表/版本/预览路径）
- 快捷键管理（列表/自定义/冲突提示）
- 文件树管理（排序/过滤/隐藏文件/状态标记/操作提示）
- 括号对彩虹高亮与当前括号对标记
- 运行时文件集中到用户级 runtime 目录（不在工作目录散落 session/swap）
- 可扩展语法高亮（内置多语言 + 自定义 language map + :syntax reload）
- Python `tokenize` 高亮 + 可配置 regex 规则（`syntax\\regex_rules.json`）
- Git 命令增强（status/diff/blame/stage/unstage/branches/checkout）
- 跳转历史（:jump back/forward/list，gb/gf，Ctrl+O）
- 配置热重载与按文件类型快捷键覆盖（无需重启）
- 撤销树分支切换（`g-` / `g+`）与主题快速切换（`:theme`）
- 终端能力探测与降级（True Color / Unicode 自动回退）
- Quickfix 列表导航（`:quickfix fromgrep|fromdiag|next|prev|list`）
- Autocmd 事件触发（`bufreadpre/post`、`bufwritepre/post`）
- 多作用域变量与剪贴板命令（`:var`、`:clip`）
- 轻量 DAP（pdb）调试管理（`:dap start/stop/continue/next/step/break`）

## 启动

```bash
python pvim.py
python pvim.py your_file.py
python pvim.py .
python pvim.py your_file.py --config pvim.config.json
```

## LSP（可选）

在 `pvim.config.json` 的 `features.lsp` 中设置 `command` 后可启用：

- `gd`：跳转定义（优先 LSP，失败回退本地搜索）
- `K`：悬浮文档
- `:lsp refs|impl|symbols|wsymbol`：引用/实现/符号检索
- `:lsp rename <name>`：基于 LSP 的符号重命名
- `:lsp format`：基于 LSP 的文档格式化
- `:lsp status|start|stop`：查看/控制 LSP 会话
- `:diag`：查看当前文件的 LSP 诊断列表

## 目录

```text
src/
  core/
  features/
  scripting/
  plugins/
  ui/
doc/
```

详细文档见 [doc/README.md](./doc/README.md)，推荐先读 [详细使用手册](./doc/getting-started/user-manual.md)。

## 测试

```bash
python -m tests.run_tests
```

## License

本项目以 **GNU GPL v3.0 (or later)** 开源发布。  
你可以在遵守 GPL 条款的前提下自由使用、修改和再分发本项目。
