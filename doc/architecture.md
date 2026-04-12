# 架构说明

## 目录结构（v0.1）

```text
pvi.py
pvi_app/
  core/
    config.py
    console.py
    theme.py
  features/
    syntax.py
    file_index.py
    fuzzy.py
    git_status.py
    refactor.py
    formatter.py
  scripting/
    lexer.py
    parser.py
    ast_nodes.py
    interpreter.py
    errors.py
  plugins/
    manager.py
  ui/
    editor.py
  main.py
```

## 分层职责

- `core`：配置、终端输入输出、主题样式。
- `features`：编辑能力与工具能力（语法高亮、模糊搜索、Git、重构、格式化等）。
- `scripting`：自定义脚本语言实现（词法、语法、AST、解释执行、安全限制）。
- `plugins`：插件发现、安装、加载、运行、宿主 API 网关。
- `ui`：编辑器主循环、渲染、交互命令与快捷键处理。

## 脚本运行链路

1. `plugins/manager.py` 读取插件脚本
2. `scripting/lexer.py` 词法分析
3. `scripting/parser.py` 语法分析生成 AST（节点带行号）
4. `scripting/interpreter.py` 执行 AST（带步数限制）
5. 脚本通过 `api(pvim, action, ...)` 与编辑器交互

## 性能策略

- 语法配置按需加载（按扩展名）
- 文件索引缓存
- Git 状态按间隔刷新
- 差量渲染（仅更新变化行）
