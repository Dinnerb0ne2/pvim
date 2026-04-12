# 快速开始

## 1. 环境要求

- Windows 终端
- Python 3.14.3
- 不需要安装任何第三方库

## 2. 启动

```bash
python pvi.py
python pvi.py your_file.py
python pvi.py your_file.py --config pvi.config.json
```

## 3. 首次体验建议

1. 启动后按 `F1` 查看快捷键提示面板。
2. 按 `Ctrl+P` 打开模糊搜索器快速切换文件。
3. 按 `Ctrl+/` 试一键注释。
4. 用 `:plugin list` 查看默认插件加载状态。

## 4. 常用命令

- `:w` 保存
- `:q` 退出
- `:find <text>` 查找
- `:replace <old> <new>` 替换下一个
- `:replaceall <old> <new>` 全量替换
- `:rename <old> <new>` 重命名符号
- `:format` 代码风格归一化
- `:plugin list` / `:plugin load`
- `:script run <path>`
