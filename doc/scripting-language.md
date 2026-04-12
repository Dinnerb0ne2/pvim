# 脚本语言（PVIM Script）

## 语法原则

- 使用 `{}` 定义代码块，不依赖缩进。
- 支持匿名函数与闭包。
- 支持字符串插值：`f"row={row}"`。

## 示例

```pvi
fn make_adder(base) {
  return fn(v) { return base + v; };
}

let add2 = make_adder(2);
api(pvim, "message", f"sum={add2(5)}");
```

## 安全约束

1. 解释执行有步数上限（`step_limit`），防止死循环。
2. 运行时异常带行号，并在编辑器弹层展示，不会直接退出主程序。
3. 插件之间独立作用域，互不污染。
4. 脚本访问宿主仅通过 `api(id, action, ...)`。

## 内置原生函数（Python 端）

- `split`, `join`, `sort`
- `len`, `str`, `int`, `float`
- `replace`, `contains`, `starts_with`, `ends_with`

## 宿主 API（节选）

- `api(pvim, "message", text)`
- `api(pvim, "virtual.add", line, text)`
- `api(pvim, "proc.start", command)`
- `api(pvim, "proc.read", pid, max_lines)`
- `api(pvim, "ast.node_at", row, col, "function,class")`

> 若需要多语言高精度 AST，安装可选依赖：`pip install tree_sitter tree_sitter_languages`。
