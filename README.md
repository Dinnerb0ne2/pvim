# PVI (Python Vim Interface)

一个基于 **Python 3.14.3** 的现代化 CLI Vim/Neovim 风格编辑器，**纯标准库实现**，不依赖任何第三方包。

## 特性

- 纯标准库（`msvcrt` + `ctypes` + ANSI VT）
- Vim 风格模态：`NORMAL` / `INSERT` / `COMMAND`
- 状态栏（模式、文件、修改状态、光标位置）
- 命令行（`:`）支持常见 Ex 命令
- 行号 + 滚动视口 + 翻页
- 差量渲染（只重绘变化行），避免全屏清空重绘导致的闪动

## 启动

```bash
python pvi.py
python pvi.py your_file
```

## 常用按键

- `i` 进入 INSERT
- `Esc` 回到 NORMAL
- `:` 进入命令行
- `h/j/k/l` 或方向键移动
- `Ctrl+S` 保存
- `Ctrl+Q` 退出（有未保存改动会阻止）
- `F2` 切换行号

## 常用命令

- `:w` 保存
- `:w <path>` 另存为
- `:q` 退出（有改动会阻止）
- `:q!` 强制退出
- `:wq` / `:x` 保存并退出
- `:e <path>` 打开文件
- `:e! <path>` 强制打开文件（忽略未保存改动）
- `:set number` / `:set nonumber` 切换行号
- `:help` 查看命令提示

## 无闪动实现要点

- 使用 Win32 Console 的 VT 模式与 ANSI 控制序列
- 使用备用屏幕缓冲（alternate screen）
- 维护上一帧缓存，仅更新发生变化的行
