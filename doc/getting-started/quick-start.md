# 快速开始

## 环境要求

- Windows 终端
- Python 3.14.3

## 启动

```bash
python pvim.py
python pvim.py your_file.py
python pvim.py your_file.py --config pvim.config.json
```

## 首次使用建议

1. `F1` 查看快捷键提示
2. `:e <file>` 打开文件
3. `i` 进入插入模式，`Esc` 回普通模式
4. `:w` 保存，`:q` 退出

## 常用命令

- `:find <text>` / `:replace <old> <new>` / `:replaceall <old> <new>`
- `:fuzzy [query]`
- `:grep <query>`（全局搜索）
- `:tree open|refresh|close|toggle`
- `:session save|load`
- `:swap write|clear`
