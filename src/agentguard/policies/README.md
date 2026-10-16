# Policies

放置可独立启用和测试的策略实现：预算、Token、限流、熔断、质量、安全、缓存策略和工具行为检查。

策略只负责“是否允许以及采取什么动作”的判断；调用顺序、重试和资源清理由 `application` 负责。

## 本地工具治理

`ToolGuard` 是显式启用的工具包装器，不会自动接入 `RunContext.invoke_tool`。
同一个 Guard 的所有包装器共享总调用限额；同一个 `BehaviorFingerprint`
共享步数上限，重复次数按工具名和规范化参数区分。

```python
from agentguard.tools import BehaviorFingerprint, ToolGuard

guard = ToolGuard(
    fingerprint=BehaviorFingerprint(max_steps=20, max_repeated_calls=2),
    max_calls=10,
)
guarded_tool = guard.wrap("lookup", lookup)
with agent_guard.run() as run:
    result = run.invoke_tool("lookup", guarded_tool, query="example")
```

参数绑定会统一位置参数、关键字参数和默认值，字典及集合顺序不影响指纹。
列表、元组、集合和不可变集合保持类型区别。当前支持内置标量及容器；循环引用
和自定义对象在执行前拒绝，调用方应先转换为受支持的数据类型。
无法获取签名的可调用对象按原始 args/kwargs 处理，不保证参数形式等价。

非法参数与被拒绝的调用不消耗次数；已开始但失败或取消的调用消耗次数，
不会自动重试。原生异步函数在被 await 时才检查和计数。

`RunContext.invoke_tool` 记录开始、成功/失败以及 `tool.finished` 耗时事件，
使用单调时钟，不记录参数、结果原文或异常消息。异步取消当前记录为工具失败。
单工具限额、审批、持久化幂等键、三层 timeout、流式工具生命周期仍未实现。
