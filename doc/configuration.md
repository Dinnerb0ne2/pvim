# 配置文件说明

项目根目录默认配置文件是 `pvi.config.json`。  
所有功能开关、快捷键、脚本与插件配置都在这里集中管理。

## 配置结构

```json
{
  "python": { "required": "3.14.3" },
  "editor": { "line_numbers": true, "tab_size": 4 },
  "theme": { "enabled": true, "config_file": "pvi.theme.default.json" },
  "features": {
    "syntax_highlighting": { "enabled": true, "language_map_file": "syntax\\languages.json", "default_file": "syntax\\plaintext.json" },
    "auto_pairs": { "enabled": true, "config_file": "autopairs.json" },
    "sidebar": { "enabled": true, "width": 30, "max_files": 3000 },
    "vscode_shortcuts": { "enabled": true, "bindings": {} },
    "key_hints": { "enabled": true, "trigger": "F1" },
    "fuzzy_finder": { "enabled": true },
    "scripting": { "enabled": true, "step_limit": 100000 },
    "plugins": { "enabled": true, "directory": "plugins", "auto_load": true },
    "git_status": { "enabled": true, "refresh_seconds": 2.0 },
    "refactor_tools": { "enabled": true },
    "find_replace": { "enabled": true },
    "code_style_normalizer": { "enabled": true }
  }
}
```

## 关键项建议

1. `features.scripting.step_limit`  
   强烈建议保持 `100000` 或更低，避免脚本死循环卡住界面。
2. `features.plugins.directory`  
   插件安装目录，默认是项目根目录下的 `plugins`。
3. `features.vscode_shortcuts.bindings`  
   所有快捷键都可重映射，例如将 `fuzzy_finder` 改为 `F5`。

## 热重载配置

在编辑器里执行：

```vim
:reload-config
```

会重新读取配置并生效（包括主题、功能开关、插件设置）。
