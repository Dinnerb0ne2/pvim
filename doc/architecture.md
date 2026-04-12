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
    modules/
      file_tree.py
      tab_completion.py
      git_control.py
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
    layout/
      component.py
      feature_registry.py
      manager.py
```

## 核心链路

1. `editor.py` 维护主循环，按键读取与渲染始终非阻塞。
2. `AsyncRuntime` 在后台线程运行 `asyncio` 循环，承载文件树/ Git 刷新和外部进程任务。
3. `LayoutManager + FeatureRegistry` 负责 Tabline / Winbar / Statusline 的空间分配与动态开关。
4. 文件树、补全、Git 控制通过 `features` 模块接入，禁用后自动退化到最简界面。
5. Buffer 提供真实文本与虚拟文本叠加层（如 Git 行标记）。
6. 脚本系统独立执行环境，错误只弹窗，不影响主程序。
