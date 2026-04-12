# 开发与版本控制

## 版本策略

- 当前开发版本：`v0.1.0`
- 阶段标签：`v0.1`

## Git 使用建议

1. 创建功能分支开发，不直接在主分支堆叠大改动。
2. 每个提交聚焦一个主题（例如“脚本解释器”或“插件安装”）。
3. 提交前至少保证可编译运行。

## 推荐流程

```bash
git checkout -b feat/scripting-runtime
# 开发与测试
git add .
git commit -m "feat: add pviscript interpreter and plugin manager"
```

## 忽略文件

项目已提供 `.gitignore`，会忽略：

- Python 缓存与字节码
- 虚拟环境目录
- IDE 临时文件
- 日志与本地构建残留
