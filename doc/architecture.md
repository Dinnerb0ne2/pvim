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
2. 输入路径使用预测性渲染：按键后立即重绘光标/文本，异步状态更新以低优先级微任务回填。
3. `AsyncRuntime` 在后台线程运行 `asyncio` 循环，承载文件树/ Git 刷新和外部进程任务。
4. 文件树和 Git 任务使用“请求版本号”机制：旧任务结果自动丢弃，只接收最新版本。
5. `LayoutManager + FeatureRegistry` 负责 Tabline / Winbar / Statusline 的空间分配与动态开关。
6. Buffer 引入 PieceTable 基础结构，用于大文件编辑路径的底层演进。
7. 启动阶段执行终端能力探测（True Color / Unicode），自动选择颜色与边框降级方案。
8. 文件树、补全、Git 控制通过 `features` 模块接入，禁用后自动退化到最简界面。
9. Buffer 提供真实文本与虚拟文本叠加层（如 Git 行标记）。
10. 脚本系统独立执行环境，错误只弹窗，不影响主程序。
