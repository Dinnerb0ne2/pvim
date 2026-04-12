# PVI (Python Vim Interface) - v0.1

PVI 是一个 **Python 3.14.3**、**纯标准库**实现的终端编辑器。

## 快速启动

```bash
python pvi.py
python pvi.py your_file.py
python pvi.py your_file.py --config pvi.config.json
```

## 主要能力

- 二级模块化目录（`core / features / scripting / plugins / ui`）
- 语法高亮（按语言配置文件加载）
- 自动补全（括号/引号）
- 侧边栏 + Git 状态 + 模糊搜索
- VSCode 风格关键快捷能力（注释、多光标、按词移动、缩进）
- 快捷键提示面板（默认 `F1`）
- 自定义脚本语言（PVIScript）与解释器
- 插件系统（发现、加载、安装、运行）
- 查找替换、重构、代码风格归一化

## 关键目录

```text
pvi.py
pvi.config.json
plugins/
doc/
pvi_app/
  core/
  features/
  scripting/
  plugins/
  ui/
  main.py
```

## 文档

完整文档在 `doc/`，入口见：

- [doc/README.md](./doc/README.md)
