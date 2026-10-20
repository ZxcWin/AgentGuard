# 运行时使用与边界

适用于 2026-10-19 当前实现。完整路线图见项目根目录 `IMPLEMENTATION_PLAN.md`；本页仅描述已经实现的行为。

## 生产严格配置

开发默认行为保持兼容；显式使用 `AgentGuardConfig.for_production(values)` 启用严格预设。调用方必须提供正整数 `token.max_output_tokens`、非负 `token.run_token_budget`、非负 `budget.run_cost_usd` 和完整模型价格；零 Run 预算表示禁止消费。示例价格仅用于演示，应替换为实际供应商价格：

```python
from agentguard import AgentGuard, AgentGuardConfig

config = AgentGuardConfig.for_production({
    "token": {"max_output_tokens": 128, "run_token_budget": 2000},
    "budget": {"run_cost_usd": 0.01},
    "pricing": {"version": "example-v1", "models": {
        "local-example": {"input_per_million": 1, "output_per_million": 2},
    }},
})
guard = AgentGuard(config)
```

预设启用 `enforcement.profile=production`、`token.missing_price=reject`、缺失 usage 估算和密钥阻断。生产模式禁止未知 usage 零记账、通配模型价格、关闭密钥检测或改为仅观测，以及 Redis admission 的 memory 故障回退。各模型输入/输出单价必须显式填写，允许显式零价；不支持将缺失字段隐式当作免费。已有配置中的不安全显式值会报错，不会被预设静默修正。可显式选择预算降级或流停止，仍受父预算约束。

YAML、环境变量、策略持久化及热更新使用相同校验。直接写 Mapping/YAML 时，须同时填写 `enforcement.profile: production` 及预设所需字段；例如环境变量 `AGENTGUARD_TOKEN__MISSING_PRICE=observe` 不能覆盖生产模式约束。更新失败保留旧版本；已有 Run 使用创建时的快照。控制台“防护策略 → 执行配置”可选择生产严格模式，自动准备缺价拒绝、缺失 usage 估算和安全阻断，预算和价格仍需填写后才能保存。

`missing_price=reject` 在供应商与 admission 执行前拒绝缺价调用（开发模式显式设置时也适用）。严格模式支持 `max_output_tokens`、`max_completion_tokens`、`max_tokens`；显式值必须为正整数且每次只能选择一种，保留更严格的用户值，并在请求拦截器之后重新限制。无法接收输出上限的入口会被拒绝。传入 `**kwargs` 的供应商适配器仍须实际遵守该上限，SDK 无法证明任意自定义函数会执行限制。

已返回响应因缺失/超额 usage 被拒绝时，先按实际 usage 或保守预估结算，再拒绝响应，不会退还已产生的消费。缓存 token 未提供折扣价时按普通输入价计费；显式零推理价会保留。

该预设提供本地配置约束，不代表全系统生产就绪。Token 采用字符估算，供应商在途/隐藏用量可能偏离预留，同步调用不能抢占中断。共享预算、审计、远程策略和账本可按需组合，生产部署仍须独立验收，见 OPERATIONS.md。可离线运行 `python examples/production_profile.py` 查看已计费成功调用与调用前缺价阻断。

## 故障熔断

`circuit` 策略控制当前 Run 的模型和工具熔断器，默认 `failure_threshold=5`、`recovery_seconds=30`、`max_keys=10000`。连续供应商/工具运行故障达到阈值后，后续同目标调用在执行前抛出 `CircuitOpen` 并记录 `circuit.rejected`。成功执行会清零故障次数；预算、安全、质量、参数校验等 SDK 拒绝不计作供应商故障。

```python
guard = AgentGuard({"circuit": {
    "failure_threshold": 2, "recovery_seconds": 10, "max_keys": 1000,
}})
```

失败阈值支持 1–1000000 的整数，作用域容量支持 1–100000 的整数，恢复时间支持 0–86400 秒的有限数；拒绝空值、布尔值、负数及非整数阈值。恢复时间为 0 表示立即允许单个恢复探测，不表示关闭熔断。恢复期后同一目标只放行一个探测；成功关闭，失败重新进入等待，取消或调用前拒绝会释放探测占用。

熔断键始终包含模型/工具种类、目标名称及配置的身份维度，与准入配额的聚合键分开。默认聚合配额下，一个工具或模型故障不会打开其他目标的熔断器。达到作用域容量上限时拒绝新增目标，不淘汰已有的打开状态。它只维护当前 Run 的内存状态，不跨 Run、进程或实例共享；新 Run 不继承先前 Run 的故障计数。

控制台“故障熔断”支持校验和持久保存三项参数，已有 Run 保留原参数快照。概览页的“生成一次熔断示例”使用当前策略和本地故障函数，最多尝试 6 次逻辑调用；已有重试、时限和预算规则仍生效。建议阈值设为 1、恢复等待大于 0、关闭自动重试后观察一次真实失败及后续 `circuit.rejected`。离线示例见 `examples/circuit_breaker.py`。

## 精确缓存

运行时精确缓存默认关闭，默认后端为进程内存储；可显式注入 SQLiteExactCache、稳定签名密钥及供应商身份实现同机跨进程/重启复用。配置、调用声明和 Run 身份三者均满足要求才会尝试缓存。持久接入见 [CACHE.md](CACHE.md)，内存离线示例见 `examples/exact_cache.py`：

```python
from agentguard import AgentGuard, CacheRequest

guard = AgentGuard({"cache": {
    "enabled": True, "ttl_seconds": 300,
    "max_entries": 256, "max_bytes": 16777216,
    "allowed_models": ["local-example"],
}})
options = CacheRequest(
    namespace="provider-deployment-v1", prompt_version="template-v1",
    authorization_context="reader-permission-v1",
    data_classification="public", pure=True,
)

def provider(**kwargs):
    return {"content": "safe answer", "usage": {"input_tokens": 4, "output_tokens": 8}}

with guard.run("example", tenant_id="local", project_id="demo",
               environment="dev", user_id="reader") as run:
    first = run.invoke(provider, model="local-example", messages="hello", cache_options=options)
    second = run.invoke(provider, model="local-example", messages="hello", cache_options=options)
```

`cache_options` 是 SDK 参数，不透传供应商。每次调用须显式声明 `pure=True` 和 `public/internal` 等级；`confidential/restricted`、未声明纯调用、无租户/项目/环境/用户身份及未在模型白名单中的调用一律绕过缓存。`namespace` 标识供应商部署/版本；`prompt_version` 标识模板版本；`authorization_context` 必须反映当前授权版本。应用须在每次调用前做权限检查，权限撤销时更换授权版本或调用 `guard.clear_cache()`，SDK 不提供身份认证或外部权限撤销通知。Run 仍使用既有策略快照，新 Run 的策略版本或内容变化会隔离旧条目。

缓存键使用 HMAC，包含完整配置及版本、身份、Agent、模型与参数、规范化输入、模板/授权/部署版本和供应商入口身份。默认随机密钥及进程内 callable 身份不跨 Guard 共享；稳定密钥模式下可通过显式 provider_id 绑定可兼容的部署入口。原输入不保存到缓存键，键和输出不写事件。存储只有有界 JSON 响应与完整性校验；命中返回新对象，调用者修改结果不会污染缓存。只支持原生字符串/字典响应及原生 JSON 参数，不转换供应商 SDK 对象，不使用任意对象的 repr 或自定义序列化。结构深度、节点和 200 KB 编码上限均有界。

永久禁止缓存敏感内容、凭据/会话/审批令牌、模型工具声明/调用内容、错误或未完成响应；请求和响应不论运行时安全开关都执行独立的密钥、邮箱、手机号和注入规则检查。此检查为启发式，不能证明所有 PII 均已识别，应用必须正确标注数据等级，不应把受保护内容声明为 public/internal。带安全/质量发现的响应即使设置 observe 也不存储。嵌套模型/工具活动会保守阻止父调用缓存；错误、取消、超时与未消费流不存储。`invoke_tool` 支持通过 `ToolCacheRequest` 显式缓存版本化纯只读 JSON 结果；要求工具白名单和 read 等级，命中仍计工具额度并经过准入、熔断与审计。写入/不可逆、要求审批或幂等键的工具不缓存。完整用法及限制见 `docs/CACHE.md`。

流式调用、配置了自动重试的调用、使用自定义请求/响应拦截器的 Run 暂时绕过缓存。可用 `CacheRequest(..., bypass=True)` 显式绕过。缓存命中仍进行请求准备及当前安全/质量检查：阻断会使条目失效并抛错；仅观测的新发现会使条目失效，按普通供应商调用继续。仅完全通过的成功调用发布条目。

命中发生在供应商 admission/预算预留之前，不调用供应商、不重复增加 `llm_calls`、Token 或成本；保留独立 `cache.hit` 事件。返回对象的 usage 仍描述原始供应商响应，不能把该值再次计入账本。未命中和后端异常都走正常限流/预算流程，异常事件不包含后端地址或异常原文。事件包括 `cache.miss/stored/hit/bypassed/error`，在控制台运行时间线可查看。

TTL 必须为 1–3600 秒，默认 300 秒；默认上限 256 条、16 MiB 编码内容，条目数可设 1–10000、容量可设 1–256 MiB。进程对象、并发请求与临时序列化还有额外内存开销，容量配置不是进程 RSS 硬上限。LRU 淘汰与过期清理受锁保护。并发相同键在本实例最多等待 1 秒并服从更短 deadline；异步等待可取消。等待超时后正常准入，允许重复供应商调用；不承诺全局 exactly-once。进行中的 owner 有 1024 个上限，超过后绕过。`guard.clear_cache()` 同时旋转密钥，清空前的在途工作不能产生之后可命中的旧条目。

`ExactCacheStore` Port、MemoryExactCache 与 SQLiteExactCache 面向本地存储，后者最多等待锁 50ms；暂不支持直接接入网络 IO 后端。默认实例 HMAC 密钥、callable 身份及单调 TTL 不跨进程恢复。callable 身份采用弱引用，最多 1024 个同时存活的入口，不为缓存长期持有闭包；不支持弱引用的入口绕过。SQLite 稳定模式使用签名 Unix TTL、持久时钟高水位、共享租约和 generation；网络共享/语义缓存及供应商对象适配仍未实现。控制台可选择启动持久后端、查看实际类型并按作用范围审计清空；演示模式可生成两个相同请求验证命中，须允许模型 `local-example` 并关闭自动重试。

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

## 内容安全与响应质量

```python
from agentguard import AgentGuard, AgentGuardConfig

guard = AgentGuard(AgentGuardConfig.from_mapping({
    "security": {
        "enabled": True, "action": "block", "detect_secrets": True,
        "detect_pii": True, "detect_prompt_injection": True, "max_chars": 200000,
    },
    "quality": {
        "enabled": True, "action": "block", "strict_json": True, "max_chars": 200000,
    },
}))
```

两组检查默认关闭，开启后的默认动作是 `block`；`observe` 仅记录发现并继续调用。配置通过控制台持久保存，新 Run 使用新快照。也可用 `AgentGuard(security_guard=SecurityGuard(...), quality_guard=QualityGuard(...))` 显式注入；注入实例优先于配置。SDK 样例见 `examples/content_guards.py`。

请求安全检查发生在请求拦截器之后、准入和预算预留之前，命中阻断时供应商不会执行。响应同时检查供应商原始内容和响应拦截器修改后的内容。规则覆盖常见 OpenAI/GitHub/Google 密钥、Bearer 令牌、凭证赋值、邮箱、中国大陆手机号，以及英文提示注入特征；它是启发式检测，不代表完整语义安全或防越权能力。遍历只读取容器字符串与标准 SDK 内容字段，不读取对象 repr 或任意属性集合。

扫描字符上限为 1–2000000；同时限制遍历深度和节点数，超限按配置动作处理，避免静默只检查前缀。流式调用扫描逐个片段与累计内容，识别跨片段规则；累计内容同样受上限约束。命中阻断会关闭底层流、结算已消耗用量、释放准入和预留，且只产生一次调用终态。已交付的片段无法撤回，因此流式检测不提供整段响应交付前的保密保证。

质量检查支持严格 JSON、Draft 2020-12 Schema、min_chars、required_substrings 和同步可插拔评估器：`QualityGuard.add_evaluator(callback)`，回调返回 `QualityResult`。Schema 需安装 `[quality]`，只允许本地引用，不读取远程 URL 或 `$id`；Schema 限制为 64 KiB、深度 32、5000 节点。Schema 的 format 默认作为注解，不启用格式断言；配置属于可信规则，不应用于执行不可信的复杂正则。JSON 检查拒绝非法结构、尾随内容及 NaN/Infinity 常量，兼容字符串、content、choices/message 内容。非流式检查在交付响应前完成；流式检查在流结束时对有界缓冲的文本执行，已交付片段无法撤回。缓冲在调用结束时清除，且不进入事件和数据库。

评估失败的 `pass/observe` 继续交付；block 阻断。`quality.action=retry` 且 `quality.max_retries>0` 时，非流式、未执行子工具的响应可重放同一模型请求，最多 10 次；每次均重新准入、结算真实或保守 usage，受父预算、retry_budget_ratio 和重试总时限约束。质量和传输错误分别计次数，共用退避配置；关闭质量重试或耗尽次数后拒绝。SDK 不自动改写 Prompt，示例见 `examples/quality_retry.py`。repair 支持应用构造新请求，degrade 在能力/地区/数据等级/价格约束下切换一次备用模型，human 需要可信审批回调及可靠审计；三者均共用质量次数和重试预算，新请求重新经过全部门禁。详见 [QUALITY.md](QUALITY.md)。流式和已执行子工具的调用不重放。检测器或评估器异常一律阻断，即使配置为 observe，且不会当作质量不合格自动重试；事件与对外异常不包含回调异常原文。自定义评估器目前为同步回调，deadline 在返回后检查，不能强制中断阻塞回调。评估器不能自动调用模型而绕过预算。

响应即使被安全或质量策略拒绝，供应商已产生的 usage 仍按原始响应结算；没有 usage 时遵循既有缺失策略。事件只保存 `request_id/stage/reason_code/action/finding_count/duration_seconds` 等元数据，质量事件带内置检查包装器版本 `evaluator_version=builtin-1`；不保存命中文本、评估器 reason 或异常原文。`security.rejected` 与 `quality.rejected` 纳入控制台拦截次数。

演示模式可在概览生成安全检测或质量检查示例：先在策略页启用对应检查，安全示例使用本地占位凭证，质量示例返回非 JSON。它们遵循当前策略，不更改开关或访问外部模型。

## 后续实现遵循的决策

[工程决策基线](DECISIONS.md) 已明确预算默认拒绝、Redis 原子共享预算、普通遥测与必需审计的故障区别、缓存默认关闭及禁止项、平台/适配器支持窗口、远程策略有效期与回滚，以及 SDK 估算和最终账单的边界。普通遥测见 [遥测文档](TELEMETRY.md)；必需审计、共享预算、已签名远程策略、角色权限与账本现已提供独立组件，注入和恢复见 [运维文档](OPERATIONS.md)。SQLite RunStore 只用于普通运行观测。

## 行为和输入用量约束

`behavior.max_repeated_outputs` 阻止达到阈值后的下一次调用，连续响应以有界内容散列比较；`behavior.max_calls_without_progress` 限制无新进展的调用数。两项默认不限制，可在策略页设置。应用确认新进展后调用 `run.mark_progress("verified-step-id")`；相同或已使用标记不重置计数，最多记住 10000 个散列。标记内容不写事件，应用负责其真实性。工具 `max_depth/max_concurrent` 约束递归与并发，取消和结束释放占用。当前重复输出检查针对非流式结果。

模型请求估算覆盖 messages/input/prompt 和 system/instructions/tools/functions/response_format；字符启发式不等同于多模态供应商 tokenizer。usage 支持平铺及嵌套 cached/reasoning 明细，明细是输入/输出子集；布尔、字符串、小数、负数、越界和不合法子集视为 unknown，按配置保守结算。真实供应商完整兼容窗口须单独验证。

## 尚未完成

非流式普通文本/JSON 已支持 security.action=redact，保留 HMAC 替换标记和结构，发生安全发现的内容不缓存；注入、敏感键、无法转换的结构和流式请求仍拒绝，详见 [CONTENT_SECURITY.md](CONTENT_SECURITY.md)。完整语义安全、网络/语义缓存、商业 SSO/跨实例待办及第三方框架专用适配器属于后续扩展。CI 已配置但尚无托管 runner 结果，真实供应商及 Redis 主备故障切换仍需环境验证。当前重试不包含流续传或工具重试；幂等和审批是本地 Run 级门禁，不等同于跨实例审批工作流。离线工具与发布校验分别见 [CLI](CLI.md) 和 [发布说明](RELEASE.md)。
