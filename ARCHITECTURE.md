# AgentGuard 架构说明

本文档定义 AgentGuard 开源 SDK 的目标架构、模块边界和演进约束。当前仓库仍处于早期骨架阶段，现有的扁平模块是过渡实现；后续开发应按本文档逐步迁移，而不是继续把所有逻辑堆到 `runtime.py`。

## 1. 设计目标

- **低延迟**：请求前决策必须是本地、可预测的同步路径；网络上报不能阻塞业务调用。
- **可组合**：预算、限流、安全、质量和缓存是独立策略，可单独启用、替换和测试。
- **供应商无关**：核心层不能导入 OpenAI、Anthropic、LangChain、Redis 或向量数据库 SDK。
- **可恢复**：异常、取消、超时、流式中断和进程退出都必须释放预留、并发槽位和临时状态。
- **可观测但安全**：事件要能关联完整调用链，但默认不记录 Prompt、工具参数和输出原文。
- **兼容演进**：公共 API、事件 schema、配置 schema 和插件协议都必须有版本策略。

## 2. 目标分层

```text
┌────────────────────────────────────────────────────────────┐
│ Public API / CLI                                           │
│ AgentGuard、RunContext、装饰器、Wrapper、命令行            │
├────────────────────────────────────────────────────────────┤
│ Integrations                                               │
│ OpenAI / Anthropic / LangChain / MCP 等薄适配器             │
├────────────────────────────────────────────────────────────┤
│ Application / Runtime                                      │
│ 调用编排、策略执行顺序、重试、降级、生命周期、取消传播      │
├────────────────────────────────────────────────────────────┤
│ Domain / Policy                                            │
│ Run、调用、Usage、预算、限流、质量、安全、决策和事件模型    │
├────────────────────────────────────────────────────────────┤
│ Ports                                                       │
│ Tokenizer、Clock、Store、Cache、EventSink、PolicyProvider   │
├────────────────────────────────────────────────────────────┤
│ Adapters                                                    │
│ 内存、SQLite、Redis、Prometheus、OTel、文件和控制平面客户端  │
└────────────────────────────────────────────────────────────┘
```

### 2.1 Domain / Policy

只包含纯数据、规则和可测试的决策函数，不进行网络、文件、线程等待或日志输出。该层负责：

- 定义不可变的请求快照、Usage、成本、预算、配额、工具调用和质量结果；
- 计算 `allow`、`deny`、`retry`、`degrade`、`stop_stream`、`human_review` 等决策；
- 保证计数、金额、时间单位、状态转换和错误码的语义一致；
- 提供策略组合时的优先级和冲突规则。

### 2.2 Ports

用 `typing.Protocol` 定义外部能力的最小接口，例如 `TokenEstimator`、`BudgetStore`、`RateLimitStore`、`Cache`、`EventSink`、`Clock` 和 `Serializer`。Port 不能依赖具体实现；应用层通过依赖注入使用它们。

### 2.3 Application / Runtime

这是唯一负责“一个调用如何执行”的层。它将请求标准化，按固定顺序执行策略，管理 reservation/settlement 生命周期，包装同步、异步和流式结果，并把供应商异常映射为结构化事件后原样重新抛出。

推荐的请求前顺序为：上下文与 deadline → 安全预检 → 请求标准化 → 缓存读取 → 熔断 → 限流/并发 → Token/成本预留 → 拦截器 → 调用供应商。请求后顺序为：消费响应 → 提取真实 usage → 质量评估 → 结算/释放 → 事件发布 → 重试或降级。

### 2.4 Adapters / Integrations

适配器只能把外部对象转换为内部 DTO，不能自行实现预算、限流或重试。每个供应商或框架适配器都要独立可选安装、独立测试，并明确支持矩阵和版本上限。适配器异常不得被宽泛捕获为“成功”。

### 2.5 Public API / CLI

公共入口只负责稳定性、参数校验和依赖组装。内部模块不得从 `agentguard.__init__` 反向导入，避免循环依赖。CLI 必须调用应用层服务，不能复制一套策略逻辑。

## 3. 推荐目录

```text
src/agentguard/
├── __init__.py                 # 仅导出稳定公共 API
├── api/                        # AgentGuard、装饰器、RunContext、Wrapper
├── domain/                     # models、errors、policy decisions、events
├── application/                # call pipeline、run service、retry/degrade
├── ports/                      # Protocol 接口和 DTO 边界
├── adapters/
│   ├── providers/               # OpenAI、Anthropic 等
│   ├── storage/                 # memory、sqlite、redis
│   ├── telemetry/               # logging、prometheus、opentelemetry
│   └── integrations/            # LangChain、MCP 等
├── policies/                    # budget、rate_limit、quality、security、loop
├── config/                      # schema、loader、env merge、validation
└── cli/                         # init、check、simulate、replay
tests/
├── unit/                        # 纯规则和状态机
├── contract/                    # Port/adapter 契约
├── integration/                 # pipeline、并发、供应商假实现
└── compatibility/               # 公共 API 和第三方返回对象
```

现有模块迁移建议：`models.py`、`exceptions.py` 进入 `domain`；`budget.py`、`limits.py`、`quality.py`、`tools.py` 进入 `policies`；`runtime.py` 拆为 `api` 与 `application`；`cache.py` 改为 `ports` 加 `adapters/storage/memory.py`；`interceptor.py` 进入 `application` 的扩展协议。当前根目录下同名 `.py` 文件仅保留兼容 re-export，不再放置业务实现；至少跨两个 minor 版本后再移除。

## 4. 依赖规则

允许依赖方向为 `api/cli → application → domain/ports`，以及 `adapters → ports/domain`。禁止 domain 导入 application、适配器或配置加载器；禁止 provider adapter 直接访问具体 storage；禁止 telemetry 反向改变策略结果。违反规则应在 CI 中通过 import-linter 或等价检查阻断。

## 5. 关键不变量

1. 一次 reservation 只能 settle 或 release 一次，且二者都必须幂等。
2. 任何请求结束（成功、异常、取消、超时、流式断开）都不能遗留预算、并发和熔断状态。
3. 请求前拒绝不能调用供应商；缓存命中不能增加 LLM 调用和供应商成本。
4. 重试必须有上限、独立预算和可识别的父子调用关系。
5. 真实 usage 缺失时必须标注估算来源，不能把估算伪装成供应商 usage。
6. 事件发布失败默认不改变业务结果，但同步审计模式必须显式配置并有背压策略。
7. `ContextVar` 只传递上下文句柄，不保存跨线程共享的可变策略状态。

## 6. 版本与兼容性

- 配置、事件和插件协议分别维护 schema version；新增字段默认向后兼容，删除或改语义必须走 deprecation 周期。
- 公共异常保留稳定 `code`、`message`、`retryable`、`request_id` 和 `run_id` 字段；原始供应商异常使用 `__cause__` 保留。
- Wrapper 只代理已确认的调用入口；未知属性、描述符、异步迭代器、上下文管理器和 `isinstance` 行为要通过兼容性测试后再承诺支持。
