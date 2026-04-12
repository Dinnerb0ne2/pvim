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

## 第一轮建议操作

1. `F1` 打开快捷键提示面板
2. `Ctrl+P` 模糊搜索文件
3. `:plugin list` 打开插件浮动列表
4. `:proc start "<command>"` 启动外部进程管道
5. `:ast` 查询光标所在函数/类边界

## 常用命令

- `:w` / `:q` / `:wq`
- `:find` / `:replace` / `:replaceall`
- `:format`
- `:virtual add <line> <text>`
- `:proc start|read|write|stop|status ...`
- `:script run <file>`
- `:profile script <file>`
