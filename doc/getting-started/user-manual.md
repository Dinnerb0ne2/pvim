# PVIM 使用手册（详细版）

## 1. 启动与打开目标

```bash
python pvim.py
python pvim.py main.py
python pvim.py .
python pvim.py D:\code\my_project
python pvim.py --config pvim.config.json
```

- 传入**文件**：进入单文件编辑模式（默认不显示侧栏）。
- 传入**文件夹**：进入项目模式（可直接使用文件树、模糊搜索、全局搜索）。

## 2. 基础模式

- 普通模式：默认模式，移动/命令/对象操作。
- 插入模式：`i` 进入，`Esc` 返回普通模式。
- 命令模式：`:` 进入，执行 `:w`、`:q`、`:e` 等命令。
- 终端模式：`:term` 进入内置终端面板，`Esc` 返回编辑器。

## 3. 常用快捷键

- 保存与退出：`Ctrl+S`、`Ctrl+Q`
- 搜索与导航：`Ctrl+P`（模糊查找）、`:grep <text>`（全局搜索）、`gd`（转到定义）
- 提示与面板：`F1`（快捷键提示）、`F3`（文件树）、`F4`（侧栏）
- 编辑：`u`（撤销）、`Ctrl+Y`（重做）、`Tab/Shift+Tab`（缩进）
- 文本对象：`ciw`、`da"`、`vap`、`cif`
- 宏：`qa ... q` 录制，`@a` 回放

## 4. 文件与项目工作流

- `:e <path>`：打开文件（也支持目录，等价项目打开）
- `:project <dir>`：打开项目目录
- `:workspace`：查看当前工作区根目录
- `:tree open|refresh|close|toggle`：文件树控制

## 5. 搜索与替换

### 普通文本

- `:find <text>`
- `:replace <old> <new>`
- `:replaceall <old> <new>`

### 正则表达式

- `:findre <pattern> [flags]`
- `:replacere <pattern> <replacement> [flags]`
- `:replaceallre <pattern> <replacement> [flags]`

可用 flags：
- `i` 忽略大小写
- `m` 多行模式
- `s` DOTALL 模式

示例：

```vim
:findre "\bTODO\b" i
:replaceallre "foo_(\d+)" "bar_\1"
```

## 6. 内置终端

- `:term`：使用系统默认 shell（Windows 为 `cmd`，Unix 为 `$SHELL`）。
- `:term <command>`：直接运行指定命令。
- 终端模式按键：
  - `Enter` 发送输入
  - `Backspace` 删除输入
  - `PgUp/PgDn` 滚动终端输出
  - `Ctrl+C` 发送中断字符
  - `Ctrl+Q` 请求停止终端进程
  - `Esc` 退出终端面板（进程可继续运行）

## 7. 编码与换行

- 自动按配置顺序尝试多编码打开文件（`utf-8`, `utf-8-sig`, `gb18030`, `gbk`, `big5`, `shift_jis`, `latin-1`）。
- 状态栏右侧显示当前编码。
- `:encoding` 查看当前编码。
- `:encoding <name>` 或 `:set encoding <name>` 切换保存编码。
- 内存统一使用 `\n`，保存时按配置保留/转换行尾（LF/CRLF）。

## 8. 分屏窗口管理

- `:split` / `:vsplit`：开启水平/垂直分屏。
- `:only`：关闭分屏，回到单窗口。
- `:wincmd w`：在主/副窗口切换焦点。
- `:wincmd h` / `:wincmd l`：缩小/扩大分屏比例。
- 普通模式快速键：
  - `Ctrl+W v`：垂直分屏
  - `Ctrl+W s`：水平分屏
  - `Ctrl+W w`：切换焦点
  - `Ctrl+W h` / `Ctrl+W l`：调比例
  - `Ctrl+W q`：关闭分屏

## 9. 数据安全（强烈建议开启）

- Swap 崩溃恢复：异常退出后下次打开同名文件会提示恢复。
- 会话恢复：保留当前文件、光标、标签页、工作区。
- 自动保存：按 `features.auto_save.interval_seconds` 周期自动落盘（默认开启）。

## 10. LSP（可选）

在 `pvim.config.json` 设置 `features.lsp.command` 后可用：

- `gd` 跳转定义
- `K` 悬浮文档
- `:lsp status|start|stop`
- `:diag` 查看诊断

## 11. 终端兼容与排障

- `:termcaps` 查看终端能力（真彩/颜色级别/Unicode）。
- 如果边框字符异常：更换支持 UTF-8 的终端字体，或使用非 Unicode 回退字符。
- 如果快捷键不生效：先按 `F1` 查看当前映射，再检查 `pvim.config.json` 的 `vscode_shortcuts`。
- 如果误触模式导致输入异常：按 `Esc` 回普通模式，再继续操作。
