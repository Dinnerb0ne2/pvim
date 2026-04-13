# 快捷键

## 基础

- `Ctrl+S` 保存
- `Ctrl+Q` / `Ctrl+C` 退出
- `F1` 快捷键提示

## 编辑

- `u` 撤销
- `Ctrl+Y` 重做
- `Ctrl+/` 注释切换
- `Tab` / `Shift+Tab` 缩进/反缩进
- `Ctrl+Left` / `Ctrl+Right` 单词跳转

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
- `ga`：代码动作（LSP）
- `Ctrl+P`：fuzzy 文件搜索
- `:grep <query>`：全局实时搜索
- `:findre <pattern> [flags]`：正则搜索
- `:replaceallre <pattern> <replacement> [flags]`：正则全量替换

## 项目与终端

- `:project <dir>`：打开目录工作区
- `:term [command]`：打开内置终端面板

## 分屏

- `Ctrl+W v`：垂直分屏
- `Ctrl+W s`：水平分屏
- `Ctrl+W w`：切换焦点窗口
- `Ctrl+W h` / `Ctrl+W l`：调整分屏比例
- `Ctrl+W q`：关闭分屏
