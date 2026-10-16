# 运行时使用与边界

适用于 2026-10-16 当前实现。完整路线图见项目根目录 `IMPLEMENTATION_PLAN.md`；本页仅描述已经实现的行为。

## 配置 Run、模型和工具

```python
from agentguard import AgentGuard

guard = AgentGuard(config={
    "timeout": {"run_seconds": 60, "llm_seconds": 20, "tool_seconds": 5},
    "tools": {
        "max_calls": 10,
        "max_steps": 10,
        "max_repeated_calls": 2,
        "per_tool_limits": {"lookup": 3},
        "side_effects": {"lookup": "read", "send": "irreversible"},
    },
})

with guard.run(name="local-agent") as run:
    result = run.invoke_tool("lookup", lambda query="demo": {"found": True})
    response = run.invoke(
        lambda **kwargs: {"usage": {"input_tokens": 8, "output_tokens": 4}},
        model="local-example", messages="local example",
    )
print(run.state.summary())
```

所有 timeout 和总工具限额默认 `None`，表示不启用；设为 `0` 表示不允许执行。时间允许有限的非负浮点数，拒绝布尔值、负数、NaN、Infinity。单工具映射中的次数必须是非负整数。配置来自 Mapping/YAML/环境变量，沿用既有加载和快照规则。

`max_calls` 是 Run 内工具总准入次数；`max_steps` 当前也按工具准入计步，不解析 Agent 内部推理过程。重复检查按工具名与签名绑定后的参数计数，显式参数与默认参数会规范化。失败/已执行调用不会退还工具次数，避免通过异常绕过上限。总次数、单工具次数和重复指纹检查在工具函数执行前完成。循环检查开启时不支持任意自定义对象或循环引用参数，无法规范化时直接拒绝。

副作用等级为 `read`、`write`、`irreversible`，未配置的工具按 `write` 处理。结果摘要只包含内建类型和长度；不写入结果内容、参数、`repr` 或对象自定义序列化。指纹以 SHA-256 散列作为键。可通过 `tools.idempotency_required_for` 要求指定工具提供幂等键；原始键不落盘，只保存 SHA-256 摘要，重复键在副作用前阻断。可通过 `tools.approval_required_for` 要求审批回调，支持同步/异步回调、拒绝、取消和协作式超时；审批请求、结果和拒绝原因写入脱敏事件。工具不会自动重试。

## 工具幂等键与审批

```python
guard = AgentGuard({
    "tools": {
        "idempotency_required_for": ["send"],
        "approval_required_for": ["send"],
        "approval_timeout_seconds": 30,
        "side_effects": {"send": "irreversible"},
    },
    "timeout": {"run_seconds": 60, "tool_seconds": 40},
})

with guard.run(name="approved-send") as run:
    result = run.invoke_tool(
        "send", lambda: {"sent": True},
        idempotency_key="business-operation-123",
        approval=lambda tool_name, key_hash: True,  # 接入应用自己的可信审批决定
    )
```

审批回调接受零个参数，或 `(tool_name, key_hash)` 两个参数；仅返回布尔值 `True` 才放行。回调异常原样传播，且不会重试回调；事件不记录异常原文。异步工具可使用同步或异步审批回调，同步工具拒绝异步审批。审批时限包含回调执行和等待，与更短的 Run/tool deadline 合并；同步回调在前后检查时限，异步回调依赖协作取消。`approval_timeout_seconds` 默认 `None`，此时继承父时限；需要有界等待时应明确设置此值或 Run/tool 时限。

幂等键必须是 1–256 个字符的非空白字符串，同一 Run 内所有工具共享键空间。审批拒绝、超时、取消或回调失败不会消耗键，审批通过后原子占用，后续即使执行失败也不退还，避免重放已经发生的副作用。不同 Run 或进程不共享键；需要跨 Run 幂等时，业务工具必须自行使用持久化幂等接口。SDK 不缓存并返回上次结果，重复键抛出 `LoopDetected`。

审批缺失或拒绝抛出 `ToolApprovalDenied`，时限到达抛出 `ExecutionTimeout` 并保留 `scope`。审计事件包括 `tool.approval_requested`、`tool.approval_resolved` 和 `tool.rejected`，后者通过 `reason_code` 区分缺失/非法/重复键及审批拒绝/超时/取消/失败。被拒绝的操作不会执行工具函数，控制台可通过事件时间线和拦截计数查看结果。配置只要求调用方提供可信审批决定，不提供审批人的身份验证或浏览器审批队列。

## 同步流

```python
with guard.run(name="sync-stream") as run:
    with run.invoke(lambda **kwargs: iter(["hello", " world"]), stream=True) as stream:
        for chunk in stream:
            print(chunk)
            break
```

显式 `stream=True` 才把可迭代返回值视为流，普通列表/字典保留原响应语义。消费结束或 `close()` 会向底层传播关闭；提前关闭同样释放预算预留与并发槽位，已经消费的部分按供应商 usage 或估算结算。供应商累计 usage 覆盖前一次累计值，不重复求和。流式 usage 在 chunk 拦截器修改内容前提取；普通响应的计费 usage 也不会被响应拦截器返回的新对象替换。

`Run` 退出会关闭尚未消费的流及未完成调用。`@guard.guard` 和 `guard.wrap(client)` 自动创建的 Run 在异步/流式结果完成后结束，不在函数刚返回协程或流时结束；使用自动 Run 的流也应显式关闭，不依赖垃圾回收。

## 异步与取消

```python
import asyncio
from agentguard import AgentGuard

async def provider(**kwargs):
    async def chunks():
        yield "hello"
        await asyncio.sleep(0)
        yield " world"
    return chunks()

async def main():
    guard = AgentGuard(config={"timeout": {"run_seconds": 30, "llm_seconds": 5}})
    async with guard.run(name="async-stream") as run:
        stream = await run.invoke(provider, stream=True)
        async with stream:
            async for chunk in stream:
                print(chunk)
                break

asyncio.run(main())
```

异步业务使用 `async with guard.run(...)`，确保退出时等待底层 `aclose()` 和取消任务的清理完成。普通 `with` 在已有事件循环内只能调度异步关闭，不能同步等待它完成。关闭尚未 await 的调用不会执行工具函数；异步函数的准入等待使用可取消的异步轮询，不用 `time.sleep` 阻塞事件循环。包装异步入口仍可通过 `inspect.iscoroutinefunction` 判断。

不要把 Run 内创建的任务或流留到 Run 退出之后执行；关闭的 Run 拒绝新调用，RunContext 不可重复进入。并发工具与模型共享 admission 限额；父调用持有槽位时发起子调用需要足够的并发额度，设置为 1 时不能假定嵌套调用可以重入占用同一槽位。

## deadline 语义

Run 使用单调时钟捕获总 deadline；LLM/tool deadline 继承调用链中更短的 deadline，子 Run 和子调用都不能延长父调用期限。LLM 时限包含请求准备、准入和响应/流消费；工具时限包含准入与执行。同步代码只能在检查点发现过期，不能在执行期间被强制中断。

- 异步等待通过 `asyncio.wait_for` 协作取消；供应商必须响应取消。拒绝取消或无限阻塞的 `aclose()` 可能让实际返回晚于 deadline。
- 同步供应商/工具在执行前后检查，超时抛出 `ExecutionTimeout`，不额外开线程执行后失去追踪。已返回的可关闭同步响应在因超时丢弃前尝试关闭。
- Run 内任意用户代码不会被强制抢占；例如用户自己的同步阻塞或在 SDK 调用外的异步等待，仅在下一个检查点或 Run 退出时发现过期。
- 写入/不可逆工具超时记录 `execution_may_have_completed=true`；这不是“未执行”的证明，不自动重试或撤销副作用。需要业务自行核对结果或使用幂等接口。

异常离开 Run 时状态为 `failed`、`cancelled` 或 `timed_out`；调用异常如果被业务代码在 Run 内捕获，Run 自身仍可正常完成，失败/超时调用事件保留。`llm.first_token` 表示首个成功消费的 chunk 耗时，不保证供应商 chunk 含可见文本。当前不提供硬线程终止、进程隔离或取消不合作实现的时限保证。

## 控制台与本地策略共享

`examples/console_agent.py` 演示 SDK 同时连接 `SQLiteRunStore` 和 `SQLitePolicyStore`。两者指向控制台同一个本地数据库的绝对路径时，新建 Run 会读取最新配置与版本；仅连接 RunStore 只上传运行记录，不读取控制台策略。已有 Run 保留创建时快照。

SQLite 策略保存采用事务内比较 `expected_version`；过期版本拒绝写入，磁盘写入失败不发布新内存快照。数据不是跨机器控制平面，也不应把 SQLite 共享文件部署视为分布式协调服务。数据库内已有策略优先于新建 ConfigManager 时的初始配置；需要改变策略时显式 update/replace，不通过重启覆盖。

运行记录在 Run 进入和退出时同步保存；存储失败按 fail-open 记录通用警告。没有后台事件队列、逐事件推送或崩溃恢复，进程异常退出可能留下 running 记录。控制台调用树仅基于已经采集并加载的事件，历史数据缺失的父子关系不能重建。

## 可选模型重试

```python
guard = AgentGuard(config={
    "retry": {
        "max_retries": 2,
        "initial_delay_seconds": 0.5,
        "max_delay_seconds": 5,
        "max_elapsed_seconds": 20,
        "jitter": True,
    },
    "token": {"run_token_budget": 10000, "retry_budget_ratio": 0.2},
})
```

默认 `max_retries=0` 完全关闭自动重试；开启后仅覆盖非流式模型调用。`max_retries` 不含首次请求，最多 10 次；总耗时包含首次请求、退避和后续尝试，并受更短的 Run/LLM deadline 约束。异步调用的时限从实际 await 开始计算。同步供应商仍只在检查点检查时限，无法强制抢占。

支持内建 `ConnectionError`/`TimeoutError`，以及供应商异常的 `status_code` 或 `response.status_code` 为 408、429、500、502、503、504 的情况。其他供应商连接异常类型需要适配为上述契约；当前不声称已覆盖全部供应商 SDK。预算、准入、熔断、执行 deadline、取消、参数和拦截器错误不重试，401/403 等客户端错误也不重试。只接受来自供应商执行/等待阶段的可重试异常，避免因回调故障重复请求。

退避为封顶指数增长；开启 jitter 时在 0 到当前退避上限间随机。`Retry-After` 接受秒数和 HTTP 日期，并作为等待下限；服务器要求超过单次等待上限，或等待无法放入剩余总时限时，直接保留原异常并记录停止原因，不提前重试。无效头忽略。

每次实际尝试独立获得 admission、检查熔断并预留预算；等待退避前已释放并发与预留。第二次起使用 `purpose="retry"`，同时受父 Run 硬预算和 `retry_budget_ratio` 限制。开启重试后，供应商失败且无法确认实际 usage 的请求会按请求前保守估算计入预算，并记录 `budget.failed_attempt_estimated`；这可能高估拒绝请求的费用，用于避免失败重试绕过预算，不代表供应商账单。重试次数按真正开始执行的额外供应商调用计数。

工具调用不自动重试。模型函数若已执行任何子工具，父模型尝试也停止重试，即使异常类型可重试；并发 Run 内其他工具执行也可能保守地触发此保护。任意业务函数自身的外部副作用无法自动检测，只有明确适合重放的模型调用才应启用此策略。流式创建和消费错误均不自动重试，避免重复输出。

事件记录每次尝试独立的 request_id、attempt、耗时、终态，以及 `llm.retry_scheduled` 的等待秒数与 `llm.retry_stopped` 的停止原因；不保存异常原文或 Retry-After 原始头。控制台策略页可编辑重试设置和预算比例，Run 详情显示次数与尝试序号。

演示模式的“生成一次重试示例”模拟首次连接失败，遵循当前保存的策略；关闭重试时该 Run 失败，允许至少 1 次重试且预算足够时第二次成功。该按钮不更改策略，不连接真实供应商。

## 尚未完成

质量与安全完整管线、后台遥测/指标导出、远程身份验证及发布 CI 仍按实施计划推进。当前重试策略未包含流续传、工具重试或真实供应商兼容矩阵；幂等和审批是本地 Run 级门禁，不等同于跨实例审批工作流。
