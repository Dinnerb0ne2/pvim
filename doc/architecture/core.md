# 核心架构

## 关键模块

- `src/ui/editor/editor.py`：编辑器主类与事件循环渲染
- `src/ui/editor/*.py`：按模式拆分（normal/insert/ui/commands）与文本对象纯函数
- `src/core/async_runtime.py`：后台 asyncio 任务调度
- `src/ui_grid.py`：UI 抽象协议（Cell / AbstractUI）
- `src/core/console.py`：`TerminalUI` 终端实现与按键读取（Windows/POSIX）
- `src/core/buffer.py`：文本缓冲区 + 虚拟文本 + PieceTable 同步
- `src/core/history.py`：撤销/重做快照栈
- `src/core/persistence.py`：swap/session 持久化
- `src/core/display.py`：CJK 双宽字符显示宽度计算
- `src/rpc.py`：通用 JSON-RPC 双向通信组件
- `src/features/lsp.py`：纯标准库 LSP(JSON-RPC) 客户端

## 渲染路径

1. 输入事件优先处理并立即渲染（预测性渲染）
2. 异步任务结果低优先级回填（Git/文件树/grep）
3. 脏行刷新减少无效输出

## 数据安全

- 自动 swap 持久化（可配置间隔）
- 启动时检测并提示恢复 swap
- 会话文件记录当前文件与光标位置
