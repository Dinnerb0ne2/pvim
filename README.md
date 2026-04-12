# PVIM - v0.2.x

PVIM 是一个基于 **Python 3.14.3**、纯标准库实现的终端编辑器。

## 核心能力

- asyncio 异步调度与外部进程管道（不阻塞渲染）
- Layout Manager + Feature Registry（Tabline / Winbar / Statusline 动态布局）
- 文件树、Tab 补全、Git 控制可插拔模块
- 虚拟文本叠加层与浮动窗口
- 自定义脚本语言 + 插件系统（单插件独立环境）
- Python 与 PVIM Script 语法高亮

## 启动

```bash
python pvim.py
python pvim.py your_file.py
python pvim.py your_file.py --config pvim.config.json
```

## 目录

```text
src/
  core/
  features/
  scripting/
  plugins/
  ui/
doc/
```

详细文档见 [doc/README.md](./doc/README.md)。
