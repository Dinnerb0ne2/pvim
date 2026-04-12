# 脚本语言（PVIScript）

PVIScript 是 PVI 内置脚本语言，语法轻量、运行在解释器沙箱内。

## 设计目标

1. 运行安全：脚本错误不会导致编辑器崩溃。
2. 易写易读：块结构使用 `{}`，不依赖缩进层级。
3. 可扩展：通过原生函数注册扩展能力。

## 基本语法

### 变量与表达式

```pvi
let a = 1;
let b = 2;
let c = a + b * 3;
```

### 条件与循环

```pvi
if c > 5 {
  api(pvim, "message", "c > 5");
} else {
  api(pvim, "message", "c <= 5");
}

while a < 10 {
  a = a + 1;
}
```

### 函数、匿名函数、闭包

```pvi
fn make_adder(base) {
  return fn(v) {
    return base + v;
  };
}

let add2 = make_adder(2);
api(pvim, "message", str(add2(5)));
```

### 字符串插值

```pvi
let row = 18;
api(pvim, "message", f"当前行号: {row}");
```

## 内置原生函数（Python 端注册）

- `len(x)`
- `str(x)` / `int(x)` / `float(x)`
- `split(text, sep)` / `join(sep, list)`
- `sort(list)`
- `upper(text)` / `lower(text)` / `replace(text, old, new)`
- `contains(text, sub)` / `starts_with(text, prefix)` / `ends_with(text, suffix)`
- `range(...)`
- `type_of(x)`

> 这些函数由 Python 原生实现，避免在脚本层重复造轮子。

## 与编辑器交互（Facade）

脚本通过 `api(id, action, ...)` 调用宿主能力，`pvim` 是宿主对象的**不透明 ID**。

```pvi
api(pvim, "message", "hello");
api(pvim, "open", "README.md");
api(pvim, "save");
```

可用 action（v0.1）：

- `message`
- `open`
- `save`
- `line_count`
- `get_line`
- `set_line`
- `cursor`
- `find`
- `replace_all`
- `command`
- `current_file`

## 错误定位

词法/语法/运行时错误都带行号，例如：

```text
line 3: Unexpected token: SEMICOLON
```

编辑器会以错误弹窗形式显示，而不是直接退出。
