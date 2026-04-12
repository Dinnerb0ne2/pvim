# PVIM - v0.2.x

PVIM 是一个基于 **Python 3.14.3**、纯标准库实现的终端编辑器。

## 核心能力

- asyncio 异步调度与外部进程管道（不阻塞渲染）
- 预测性渲染（输入优先，后台状态低优先级回填）
- Layout Manager + Feature Registry（Tabline / Winbar / Statusline 动态布局）
- 文件树、Tab 补全、Git 控制可插拔模块
- 子进程刷新版本号丢弃机制（只接收最新结果）
- 虚拟文本叠加层与浮动窗口
- PieceTable 底层结构（大文件编辑基础）
- 自定义脚本语言 + 插件系统（单插件独立环境）
- Python 与 PVIM Script 语法高亮
- 终端能力探测与降级（True Color / Unicode 自动回退）

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

## License

本项目以 **GNU GPL v3.0 (or later)** 开源发布。  
你可以在遵守 GPL 条款的前提下自由使用、修改和再分发本项目。
