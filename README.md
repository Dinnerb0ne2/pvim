# PVIM - v0.2

PVIM 是一个基于 **Python 3.14.3** 的终端编辑器，核心能力包括：

- 异步事件循环与外部进程管道（不阻塞 TUI）
- 脚本语言 + 插件系统
- 虚拟文本叠加层
- 通用浮动列表组件
- AST 节点查询接口（tree-sitter 集成路径 + Python AST 回退）

## 启动

```bash
python pvim.py
python pvim.py your_file.py
python pvim.py your_file.py --config pvim.config.json
```

## 目录结构

```text
src/
  core/
  features/
  scripting/
  plugins/
  ui/
pvim.py
pvim.config.json
plugins/
doc/
```

## 文档

- [doc/README.md](./doc/README.md)
