# 快速开始

## 环境要求

- Windows 终端
- Python 3.14.3

## 启动命令

```bash
python pvim.py
python pvim.py your_file.py
python pvim.py your_file.py --config pvim.config.json
```

## 首次启动

- 未打开文件时会显示 PVIM 封面页（类似 Vim 启动界面）。
- 默认只启用基础功能，高级特性需在配置中打开。

## 常用命令

- `:w` / `:q` / `:wq`
- `:find` / `:replace` / `:replaceall`
- `:format`
- `:tree open|refresh|close|toggle`
- `:feature <name> <on|off>`
- `:proc start|read|write|stop|status ...`
- `:script run <file>`
- `:profile script <file>`
