# 配置说明

主配置文件：`pvim.config.json`

## 编辑体验相关

```json
{
  "editor": {
    "soft_wrap": true,
    "default_line_ending": "lf",
    "preserve_line_ending": true
  }
}
```

- `soft_wrap`：渲染时软折行显示超长行
- `default_line_ending`：新文件默认换行符（`lf` / `crlf`）
- `preserve_line_ending`：保存时沿用原文件换行符

## 核心特性开关

```json
{
  "features": {
    "text_objects": { "enabled": true },
    "undo_tree": { "enabled": true, "max_actions": 400 },
    "macros": { "enabled": true },
    "live_grep": { "enabled": true, "max_results": 200 },
    "swap": { "enabled": true, "interval_seconds": 4.0 },
    "session": { "enabled": true, "file": ".pvim.session.json" }
  }
}
```

## 运行时命令

- `:reload-config`
- `:feature <name> <on|off>`
- `:termcaps`
- `:piece`
