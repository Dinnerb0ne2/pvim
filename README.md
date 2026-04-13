# PVIM - v0.4

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
- 分屏窗口管理（split/vsplit/wincmd）
- 内置终端面板（:term）
- 多编码读取与按编码保存（状态栏显示编码）
- Swap 恢复 + 会话恢复 + 多会话档案 + 自动保存
- 子进程刷新版本号丢弃机制（只接收最新结果）
- 虚拟文本叠加层与浮动窗口
- PieceTable 底层结构（大文件编辑基础）
- 自定义脚本语言 + 插件系统（单插件独立环境）
- 可扩展语法高亮（内置多语言 + 自定义 language map + :syntax reload）
- Git 命令增强（status/diff/blame/stage/unstage/branches/checkout）
- 跳转历史（:jump back/forward/list，gb/gf，Ctrl+O）
- 配置热重载与按文件类型快捷键覆盖（无需重启）
- 终端能力探测与降级（True Color / Unicode 自动回退）

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
