# 架构说明（v0.2）

```text
src/
  core/
    config.py
    console.py
    theme.py
    async_runtime.py
    process_pipe.py
    buffer.py
  features/
    syntax.py
    ast_query.py
    ...
  scripting/
    lexer.py
    parser.py
    ast_nodes.py
    interpreter.py
  plugins/
    manager.py
  ui/
    editor.py
    floating_list.py
```

## 核心链路

1. UI 主循环非阻塞轮询按键 + 渲染
2. 后台 asyncio 事件循环调度异步任务与进程 IO
3. Buffer 保存真实文本 + 虚拟文本叠加层
4. 脚本通过 Facade API 驱动编辑器能力
5. AST 查询优先 tree-sitter，缺失时回退 Python AST（`.py`）
