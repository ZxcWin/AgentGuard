# Application

放置用例编排和运行时生命周期：请求标准化、策略执行顺序、预算预留/结算、重试、降级、超时、取消、流式消费和事件发布。

这是唯一协调多个策略和外部 Port 的层，不应成为新的“大杂烩模块”。复杂流程应按 use case 或 pipeline 拆分。

当前运行时为每次模型调用创建 `CallContext` 和 `RequestSnapshot`，并按以下顺序调用拦截器：

调用管线的请求准备阶段已经独立封装，负责统一执行供应商请求参数限制、拦截器请求变更、
输入/输出 Token 预估、成本预估和流式预留上限；随后才进入 admission、熔断、预算预留、
供应商调用、响应消费、结算和事件发布阶段。后续阶段继续按此边界逐步拆分，保持公共
`RunContext.invoke` API 不变。

同步和异步非流式响应共享同一套结算生命周期：先捕获原供应商 usage，再执行响应拦截、校验 usage、结算
预算、记录成功并释放 admission；供应商异常、响应拦截异常、usage 校验异常和异步取消
都会统一记录失败、调用 `on_error`/`on_finish`，并释放预算预留和 admission，避免资源泄漏。

准入阶段独立负责限流获取、熔断检查和预算预留。任一检查失败都会在进入供应商调用
前终止，并释放已经取得的 admission；预算降级仍沿用当前请求的调用生命周期，最终模型
切换与独立的 fallback 规则将在后续重试/降级阶段继续完善。

调用启动阶段负责触发 `on_start`、发布 `llm.requested` 并调用供应商函数；响应分派阶段
根据返回值分别进入同步响应、异步响应、同步生成器或异步生成器路径。生成器只在客户端
开始消费时结算，协程只在真正 `await` 后结算，避免把可迭代对象或协程误当作普通响应。

1. `before_request`：可修改标准化请求，返回 `None` 时短路阻止供应商调用；
2. `on_start`：预算预留和准入成功后触发；
3. `after_response`：供应商返回后修改响应；
4. `on_error`：供应商、预算或响应处理失败时触发；
5. `on_finish`：所有终态清理完成后触发。

流式调用的每个 chunk 会依次经过 `on_chunk`；默认实现复用 `after_response`，因此可以
在不理解供应商 SDK 对象的情况下修改或脱敏 chunk。同步生成器和异步生成器都会在
正常结束、供应商异常、预算停止或客户端提前关闭后释放 reservation 和 admission lease。

每次调用都会关联 `request_id`、`trace_id`、`run_id`、`parent_id` 和 `attempt`。

`GuardedProxy` 透传同步/异步上下文管理器及同步/异步迭代协议。同一个
`AgentGuard` 重复包装已有代理时会直接返回原代理，避免形成代理嵌套。若底层对象的
`__enter__` 或 `__aenter__` 返回对象自身，代理会改为返回 `GuardedProxy`，确保上下文
内部的模型调用仍经过保护；若返回其他对象，则保持该返回值不变。代理不承诺
`isinstance`、对象身份或所有特殊方法都与底层对象完全透明。

`lifecycle.py` 管理模型调用终态、流和待 await 响应的关闭；`tool_calls.py` 管理 Run 工具限额、
结果结构摘要和副作用事件；`deadlines.py` 使用单调时钟继承更短的 Run/LLM/tool deadline。
异步 Run 应使用 `async with`；同步超时不抢占线程，异步取消依赖底层协作。
`retries.py` 编排默认关闭的非流式模型重试：每次独立 admission/reservation，退避时不占槽位；
异常分类在纯策略 `policies/retry.py`。失败且 usage 不明确时保守计入估算预算。
工具、流、已执行子工具的尝试和拦截器异常不自动重试。
具体示例和边界见项目根目录 `docs/RUNTIME.md`。能力约束 fallback 和后台遥测尚未实现。
