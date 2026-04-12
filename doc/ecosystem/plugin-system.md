# 插件系统

PVIM 插件使用 `.pvi` / `.pvs` 脚本文件，默认目录为 `plugins\`。

## 命令

- `:plugin list`
- `:plugin load [name]`
- `:plugin install <path>`
- `:plugin run <plugin> <function> [args...]`

## 要点

- 每个插件文件运行在独立环境
- 运行时错误不会导致主程序退出
- 编辑脚本时支持内置 PVIM Script 高亮
