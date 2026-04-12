# 插件系统

PVIM 插件基于脚本文件（`.pvi` / `.pvs`）运行。

## 目录与发现

- 默认目录：`plugins/`
- 支持单文件插件与目录插件（`plugin.json` + `main`）

## 命令

- `:plugin list`（浮动列表展示）
- `:plugin load [name]`
- `:plugin install <path>`
- `:plugin run <plugin> <function> [args...]`

## 生命周期

- `on_load()`：插件加载后执行
- `on_key(key)`：按键事件回调（可选）

## 执行模型

- 每个插件独立解释器与变量环境
- 插件脚本 AST 在加载时预编译并缓存
- 通过 Facade API 与编辑器交互，不直接暴露底层复杂对象
