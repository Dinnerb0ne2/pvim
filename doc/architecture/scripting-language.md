# PVIM Script

## 语法特性

- 使用 `{}` 管理代码块，不依赖缩进
- 支持匿名函数与闭包
- 支持字符串插值：`f"row={row}"`

## 安全模型

- 每次执行有步数上限（默认 `1000000`）
- 脚本异常带行号并以编辑器弹窗显示
- 插件独立作用域，互不污染
- 宿主能力仅通过 `api(id, action, ...)` 暴露

## 示例

```pvi
fn make_adder(base) {
  return fn(v) { return base + v; };
}
let add2 = make_adder(2);
api(pvim, "message", f"sum={add2(5)}");
```
