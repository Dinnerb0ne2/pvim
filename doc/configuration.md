# 配置文件说明

主配置文件：`pvim.config.json`

## 核心结构

```json
{
  "python": { "required": "3.14.3" },
  "theme": { "enabled": true, "config_file": "pvim.theme.default.json" },
  "performance": {
    "experimental_jit": true,
    "lazy_load": true,
    "profile_top_n": 25
  },
  "features": {
    "scripting": { "enabled": true, "step_limit": 100000 },
    "plugins": { "enabled": true, "directory": "plugins", "auto_load": true }
  }
}
```

## 性能相关建议

1. `performance.experimental_jit = true`  
   会设置 `PYTHON_JIT=1`（Python 3.14 实验路径）。
2. `performance.lazy_load = true`  
   延迟插件和语法解析器加载，降低启动成本。
3. `features.scripting.step_limit`  
   脚本步数上限，防止 `while true` 卡死。
4. `performance.profile_top_n`  
   控制 `:profile script` 的输出热点数量。

## 运行时重载

```vim
:reload-config
```
