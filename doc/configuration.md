# 配置文件说明

主配置文件：`pvim.config.json`

## 默认策略

- 默认是“基础模式”：高级功能（文件树、Tabline、Winbar、补全、Git 控制、通知、插件）默认关闭。
- 只保留基础编辑、语法高亮、自动配对和核心快捷键。

## 关键字段

```json
{
  "python": { "required": "3.14.3" },
  "performance": {
    "experimental_jit": true,
    "lazy_load": true,
    "profile_top_n": 25
  },
  "features": {
    "piece_table": { "enabled": true, "large_file_line_threshold": 50000 },
    "tabline": { "enabled": false },
    "winbar": { "enabled": false },
    "file_tree": { "enabled": false },
    "tab_completion": { "enabled": false },
    "git_control": { "enabled": false },
    "notifications": { "enabled": false },
    "plugins": { "enabled": false, "directory": "plugins", "auto_load": true },
    "plugin_keyhooks": { "enabled": false }
  }
}
```

> `features.scripting.step_limit` 默认提升到 `1000000`，解释器单次执行超过上限会直接抛错并弹窗，不会带崩主程序。

## 快捷键绑定配置

`features.vscode_shortcuts.bindings` 支持以下常用键位：

- `open_completion`（默认 `CTRL_N`）
- `toggle_file_tree`（默认 `F3`）
- `toggle_sidebar`（默认 `F4`）
- `toggle_comment` / `quick_find` / `quick_replace` / `fuzzy_finder` 等

## 运行时命令

- `:reload-config` 重新加载配置
- `:feature <name> <on|off>` 动态开关单个特性
- `:piece` 查看 PieceTable 当前状态
- `:termcaps` 查看终端能力探测结果

## 终端能力降级

- PVIM 启动时会探测终端色彩和 Unicode 支持能力。
- 不支持 True Color 时自动降级到 256/16 色。
- 不支持 Unicode 时浮窗边框和树形线条自动降级为 ASCII。
