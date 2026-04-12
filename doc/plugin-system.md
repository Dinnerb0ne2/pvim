# 插件系统

PVI 插件基于 PVIScript 实现，不依赖第三方库。

## 插件目录

默认目录：`plugins/`（可在 `pvi.config.json` 修改）。

支持两种插件形态：

1. 单文件插件：`plugins/name.pvi` 或 `plugins/name.pvs`
2. 目录插件：包含 `plugin.json` + 主脚本

目录插件清单示例：

```json
{
  "name": "my-plugin",
  "main": "main.pvi"
}
```

## 生命周期约定

- `fn on_load() { ... }`：插件加载后触发
- `fn on_key(key) { ... }`：按键事件触发（可选）

## 命令

- `:plugin list`：查看插件列表与加载状态
- `:plugin load`：加载全部已发现插件
- `:plugin load <name>`：加载指定插件
- `:plugin install <path>`：安装本地插件文件/目录
- `:plugin run <name> <function> [args...]`：调用插件函数

## 默认插件

项目内置示例插件：

- `plugins/welcome.pvi`

它会在加载时提示，并示例了 `on_key` 与普通函数调用。

## 隔离策略

- 每个插件一个独立解释器环境（变量不互通）
- 通过 `api(pvim, ...)` 访问宿主能力，不直接暴露 Python 内部对象
- 插件异常被捕获并弹窗，不会导致主编辑器退出
