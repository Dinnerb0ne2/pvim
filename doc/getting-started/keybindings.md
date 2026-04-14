# 快捷键

## 基础

- `Ctrl+S` 保存
- `Ctrl+Q` / `Ctrl+C` 退出
- `F1` 快捷键提示

## 编辑

- `u` 撤销
- `Ctrl+R` / `Ctrl+Y` 重做
- `g-` / `g+` 切换撤销分支
- `Ctrl+/` 注释切换
- `Ctrl+D` 选中下一个同词（基础多光标）
- `Tab` / `Shift+Tab` 缩进/反缩进
- `Ctrl+Left` / `Ctrl+Right` 单词跳转
- `%`：跳到匹配括号

## 文本对象（普通模式）

- `ciw`：修改当前单词
- `da"`：删除引号及内部内容
- `vap`：选中段落
- `cif`：修改函数内部（AST）

## 宏

- `qa ... q`：录制到寄存器 `a`
- `@a`：回放寄存器 `a`

## 跳转与搜索

- `gd`：转到定义（当前工程内）
- `gb` / `gf`：跳转历史后退 / 前进
- `Ctrl+O`：快捷后退
- `ga`：代码动作（LSP）
- `/`：增量搜索（输入即预览，Up/Down 历史）
- `n`：重复上一次搜索
- `Ctrl+P`：fuzzy 文件搜索
- `:grep <query>`：全局实时搜索
- `:findre <pattern> [flags]`：正则搜索
- `:replaceallre <pattern> <replacement> [flags]`：正则全量替换
- `:replaceproj <old> <new>`：项目级全量替换
- `:replaceprojre <pattern> <replacement> [flags]`：项目级正则替换

## 项目与终端

- `:project <dir>`：打开目录工作区
- `:tree sort <name|type|mtime>` / `:tree filter <text>` / `:tree hidden <on|off>`
- `:theme list` / `:theme <name>`
- `:term [command]`：打开内置终端面板
- `:git status|diff|blame|stage|unstage|branches|checkout`

## 分屏

- `Ctrl+W v`：垂直分屏
- `Ctrl+W s`：水平分屏
- `Ctrl+W w`：切换焦点窗口
- `Ctrl+W h` / `Ctrl+W l`：调整分屏比例
- `Ctrl+W q`：关闭分屏
