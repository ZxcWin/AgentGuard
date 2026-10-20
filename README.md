<p align="center">
  <img src="https://raw.githubusercontent.com/agentguard/agentguard/main/docs/static/logo.png" width="200" alt="AgentGuard Logo">
</p>

<h1 align="center">🛡️ AgentGuard</h1>

<p align="center">AI Agent 的实时“监护仪”与“刹车片”</p>

<p align="center">
  <a href="https://pypi.org/project/agentguard/"><img src="https://img.shields.io/pypi/pyversions/agentguard" alt="Python versions"></a>
  <a href="https://pypi.org/project/agentguard/"><img src="https://badge.fury.io/py/agentguard.svg" alt="PyPI version"></a>
  <a href="#-许可证"><img src="https://img.shields.io/badge/License-Apache%202.0-green.svg" alt="Apache 2.0 License"></a>
  <a href="#-贡献指南"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs welcome"></a>
  <a href="https://pepy.tech/project/agentguard"><img src="https://static.pepy.tech/badge/agentguard" alt="Downloads"></a>
</p>

> **一句话定义**：AgentGuard 是一个轻量级、开源的 Agent Runtime Guard，通过包裹模型、工具和 Agent 执行链，在运行时对 **Token、成本、耗时、质量、并发和调用次数**做可执行的控制与记录。

它帮助你避免 Agent 死循环、意外超支和性能瓶颈，让 Agent 从“能运行”变成“可控制、可观测、可上线”。

> **产品规划**：AgentGuard 采用“开源 SDK + 可选控制平面”的分层模式。开源部分负责靠近业务代码的实时防护和本地决策；商业部分负责组织级策略管理、跨实例治理、审计和可视化运营。下文标注为“规划中”的内容，代表产品方向，不等同于当前已实现能力。

> **闭源版说明**：闭源商业版的产品边界与部署形态见 [`CLOSED_SOURCE.md`](CLOSED_SOURCE.md)。

## 当前进度（2026-10-19）

完整已交付与未完成项见 [交付清单](docs/STATUS.md)，本地验证与安装包见 [发布说明](docs/RELEASE.md)。

控制台现支持配置熔断失败阈值、恢复等待与作用域容量；参数持久保存并作用于新 Run。模型和工具目标隔离，概览页可生成本地故障演示并查看 `circuit.rejected`。状态限于当前 Run，不跨实例共享，见 [运行时说明](docs/RUNTIME.md#故障熔断)。

新增可运行的必需审计 outbox 与幂等归档、工作空间 Bearer 身份与角色授权、签名远程策略与到期拒绝、Redis 原子共享预算，以及不可变 USD 账单对账与调整分录。控制台新增审计、成本账本两页；一条命令启动前后端：`python tools/dev.py --demo --host 192.168.3.185`。操作方法、恢复步骤和部署边界见 [运维文档](docs/OPERATIONS.md)。

新增 JSON Schema/文本规则与受预算限制的质量重试，已接入 SDK、策略持久化和前端；提供有界后台事件导出、Prometheus 指标、默认 7 天普通运行记录清理和设置页导出状态。离线 CLI 支持 init/check/explain/simulate/replay，wheel 已通过无可选依赖的干净安装检查。用法见 [CLI](docs/CLI.md)、[遥测](docs/TELEMETRY.md) 和 [发布与迁移](docs/RELEASE.md)。CI 已配置，托管跨平台结果尚未验证。

新增质量请求修复、受约束备用模型、可靠审计的应用人工批准，以及非流式 JSON 结构化脱敏。控制台可编辑动作和时限；应用负责注册修复/审批回调，后续请求仍需通过全部门禁。见 [质量恢复](docs/QUALITY.md) 与 [内容安全](docs/CONTENT_SECURITY.md)。

普通遥测支持有界批量回调、成功 trace/租户采样与可选 OpenTelemetry 终态 span；错误、拒绝和慢事件保留，指标及可靠审计不采样。应用负责中心 exporter/collector，详见 [遥测文档](docs/TELEMETRY.md)。

运行时精确缓存默认关闭，支持模型响应和显式版本化只读工具结果，使用内存或同机 SQLite。身份/授权/策略 HMAC 隔离、TTL/LRU、共享租约与命中复检已接入；工具命中仍受治理、配额和审计约束。稳定密钥和明确供应商身份支持重启复用，清空使连接实例的旧代条目失效；控制台可通过启动参数接入。见 [缓存文档](docs/CACHE.md)、[模型示例](examples/persistent_cache.py) 和 [只读工具示例](examples/readonly_tool_cache.py)。网络/语义缓存、流式/重试/SDK 对象仍未实现。

新增本地生产严格配置预设和控制台模式选择：预算/价格/安全约束在保存与启动时校验，缺价在调用前拒绝，usage 拒绝仍保留消费结算。使用方式见 [运行时文档](docs/RUNTIME.md) 和 [离线示例](examples/production_profile.py)。本地验证结果以 [发布说明](docs/RELEASE.md) 为准；严格预设不代表全系统生产就绪。

预算超限、一致性、审计、缓存、平台支持、远程策略与成本来源已在 [工程决策基线](docs/DECISIONS.md) 定案。该文档分别标明默认行为、后续验收条件与当前实现差距；决策通过不代表对应能力已生产就绪。

已完成 SDK 分层、配置校验/快照、Token/成本预算与预留结算、内存/Redis 准入限流、基础熔断，以及同步/异步/流式调用的生命周期清理。Run 已接入工具总次数、单工具额度、步数、重复调用和副作用等级配置，支持 Run/LLM/tool 三层单调 deadline。

控制台现有六个页面：运行概览、运行记录/调用树/事件详情、防护策略、审计记录、成本账本、接入设置；使用 FastAPI 版本化接口和 SQLite 脱敏运行存储。支持时间/模型筛选、分页、工具/超时/行为规则编辑、策略校验、乐观版本更新、断线重试、键盘操作和手机布局。

快速预览（项目根目录，先安装 Python 3.10+、Node.js 22+）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[console,console-test,yaml]"
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run build
.\.venv\Scripts\python.exe -m agentguard.api.http --demo
```

打开 http://127.0.0.1:8000 。演示数据明确标记，全部来自本地示例调用，不需要供应商密钥。真实数据模式去掉 `--demo`，运行 `examples/console_agent.py` 可验证 SDK → SQLite → 控制台链路。

控制台默认本机访问，支持可信局域网开发代理；远程绑定要求服务端凭证、准确 Host/Origin，生产由 HTTPS 代理保护。策略和版本保存在 SQLite；独立 SDK 显式连接同一个 SQLitePolicyStore 后，新 Run 读取最新版本。跨机器可选 RemoteConfigManager，通过已签名且未过期的快照接入；已有 Run 保持快照并在下一次准入复查有效期。模型重试默认关闭，各次尝试独立准入且共用父预算；工具和流不自动重试。同步调用无法强制中断线程或撤销副作用。真实供应商、Redis 主备切换及跨平台托管 CI 仍需环境验收，当前未向外发布。完整启动见 [控制台文档](frontend/README.md)，执行限制见 [运行时文档](docs/RUNTIME.md)。

## 🐍 技术栈

AgentGuard 核心 SDK 使用 **Python** 实现；可选本地控制台独立使用 Vue 3 + TypeScript，不影响 SDK 的独立安装和运行。

- **实现语言**：Python；
- **交付方式**：Python 包，通过 `pip` 安装和升级；
- **核心形态**：Python SDK、Wrapper、装饰器、上下文管理器和插件接口；
- **配置方式**：YAML 配置与 Python API 双重支持；
- **可选后端**：内存、Redis、SQLite，以及 Chroma、Qdrant 等 Python 生态适配器；
- **集成方式**：通过 Python 适配器接入 OpenAI、Anthropic、LangChain、LlamaIndex、FastAPI、Celery、MCP 等工具或框架。

SDK 新功能保持 Python 实现和 Python API 的一致性。控制台前端依赖在 `frontend/package.json` 中单独管理，HTTP 服务通过 `console` extra 可选安装。组织级商业控制平面的范围仍以 `CLOSED_SOURCE.md` 为准。

## 📖 目录

- [为什么需要 AgentGuard](#-为什么需要-agentguard)
- [技术栈](#-技术栈)
- [实现计划](IMPLEMENTATION_PLAN.md)
- [架构说明](ARCHITECTURE.md)
- [快速接入](#-快速接入)
- [核心功能](#-核心功能)
- [Token 控制到底如何生效](#-token-控制到底如何生效)
- [开源版与商业版](#-开源版与商业版)
- [工作原理](#-工作原理)
- [安装与开始](#-安装与开始)
- [配置参考](#-配置参考)
- [指标与事件模型](#-指标与事件模型)
- [自定义拦截器与评估器](#-自定义拦截器与评估器)
- [性能基准](#-性能基准)
- [路线图](#-路线图)
- [可扩展方向](#-可扩展方向)
- [贡献指南](#-贡献指南)
- [许可证](#-许可证)

## 🤯 为什么需要 AgentGuard

如今的 AI Agent（如 AutoGPT、MetaGPT）往往像一枚“黑箱火箭”：

- **成本不可控**：一次死循环可能在数分钟内产生高额模型费用；
- **质量不稳定**：同一个 Prompt 的输出质量可能波动很大；
- **限流难处理**：多租户高并发时，容易集中遭遇 OpenAI `429` 错误；
- **监控不够细**：只看到总耗时，却无法判断瓶颈来自 LLM、工具调用还是 RAG 检索。

AgentGuard 不是传统的“事后记账本”，而是一个运行时的**策略执行点（Policy Enforcement Point）**：

1. 在请求发出前，通过拦截器检查预算、限流和请求策略；
2. 在输出返回后，执行质量校验、重试或降级策略；
3. 在整个调用链路中记录耗时、Token、成本和工具调用信息。

## ⚡ 快速接入

### 方式一：自动 Wrap（仅需 1 行）

```python
from openai import OpenAI
from agentguard import AgentGuard

client = AgentGuard().wrap(OpenAI())

# 后续调用自动受预算、限流、质量和耗时监控保护
response = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "讲个 100 字笑话"}],
)
```

### 方式二：LangChain 回调

```python
from agentguard.integrations.langchain import GuardCallbackHandler
from langchain.agents import create_react_agent

executor = create_react_agent(...)
executor.run(input, callbacks=[GuardCallbackHandler()])
```

### 方式三：Python 装饰器

```python
from agentguard import AgentGuard

@AgentGuard(config={"budget": {"run_cost_usd": 0.01}}).guard
def my_agent_function(query):
    # 里面可以是任何代码（OpenAI、Anthropic、Requests 等）
    return client.chat.completions.create(...)
```

## 🎯 核心功能

| 能力 | 实现 | 价值 |
| --- | --- | --- |
| 💰 Token 与成本控制 | 支持 `max_tokens` 和 `max_cost_usd`；超限后可自动降级或熔断 | 避免 Agent 死循环导致意外超支 |
| 🚦 智能限流 | 支持 RPM、TPM、CPM 等多维度限流，并支持优先级队列 | 应对突发流量，降低 `429` 错误影响 |
| ⏱️ 细粒度耗时追踪 | 分别记录 LLM 推理、工具调用、RAG 检索和代码执行耗时 | 快速定位性能瓶颈 |
| 🧠 质量守卫 | 支持语义一致性检查和 JSON 格式校验；低质量结果可自动重试 | 提高生产环境输出可靠性 |
| 📦 语义级缓存 | 基于 Chroma 或 Qdrant 缓存语义相似请求 | 减少重复 LLM 调用，降低成本 |
| 🧬 Agent 行为指纹 | 检测重复工具调用和输出不变等异常模式，并主动中断 | 防止 Agent 陷入自我循环 |

## 🧮 Token 控制到底如何生效

Token 管理是 AgentGuard 的核心，不应该只是“调用结束后统计一下用了多少 Token”。一个可用的 Token Guard 至少需要同时处理**请求前限制、执行中限制和请求后结算**。

### 三种 Token 概念

| 概念 | 含义 | 能否阻止继续调用 |
| --- | --- | --- |
| `max_output_tokens` | 单次模型请求允许生成的最大输出 Token 数，最终会下发到供应商参数 | 可以限制单次输出上限 |
| `run_token_budget` | 一次 Agent Run（包括多轮模型调用、重试和工具循环）的累计 Token 预算 | 可以在下一次调用前阻止继续调用 |
| `tenant_token_quota` | 租户、应用或用户在时间窗口内可使用的 Token 配额 | 可以在请求前拒绝或排队 |

### 真正的硬控制

AgentGuard 的硬控制目标是：**预算耗尽后，不再发出下一次模型请求**，而不是调用完成后再打印一条超预算日志。

```text
Agent 请求
   │
   ├─ 1. 读取本次 Run 已消耗 Token
   ├─ 2. 估算本次请求 Token（Prompt + 历史消息 + 工具定义）
   ├─ 3. 预留预算（reservation）
   ├─ 4. 注入 max_output_tokens
   ├─ 5. 发起模型请求
   ├─ 6. 流式输出时按 Token / 字符增量检查
   ├─ 7. 使用供应商返回的 usage 做精确结算
   └─ 8. 预算不足：拒绝下一轮、降级、返回安全结果或抛出异常
```

### 预算策略

- **单次请求上限**：防止某次 Prompt 直接产生过大的输出；
- **单次 Run 上限**：限制 Agent 在 ReAct、多轮规划和自动重试中的总消耗；
- **重试独立配额**：重试不能无限复用原始预算，避免“质量守卫”反而造成超支；
- **工具循环上限**：限制单个工具、工具总次数和连续重复调用次数；
- **并发预留**：多个请求同时进入时，先预留预算，避免并发请求共同突破配额；
- **优先级策略**：预算不足时，可以让高优先级任务继续执行，低优先级任务排队或拒绝；
- **模型降级**：预算接近阈值时，将后续调用切换到更低成本模型；
- **熔断策略**：超过硬预算后立即阻止下一次调用，并返回结构化的 `BudgetExceeded` 错误。

### 重要边界

`max_output_tokens` 是对单次模型输出的上限，不等于整个 Agent Run 的总预算。AgentGuard 需要在 Agent 循环外层维护 Run 状态，才能控制多轮累计消耗。

对于非流式请求，模型已经开始生成后，SDK 通常无法撤回已经产生的远程 Token；因此最可靠的硬控制发生在**请求发出前**，以及**下一轮调用开始前**。对于流式请求，可以在达到预算时主动关闭连接，但已经产生的 Token 仍应按供应商最终 usage 或本地估算进行结算。

这一区分让指标更诚实：AgentGuard 不把“事后统计”包装成“实时中断”，而是明确告诉使用者每条策略在哪个阶段生效。

### 推荐配置

```yaml
token:
  # 单次模型请求的输出上限
  max_output_tokens: 1024

  # 一次 Agent Run 的累计 Token 硬预算
  run_token_budget: 8000

  # Prompt 过长时，请求前直接拒绝
  max_input_tokens: 12000

  # 预算预留比例，用于覆盖估算误差
  reservation_buffer: 0.1

  # 重试最多使用独立预算的 20%
  retry_budget_ratio: 0.2

  # 达到预算比例时切换备用模型
  degradation_threshold: 0.8

  # 超过预算时的行为：reject | downgrade | stop_stream
  on_exceeded: reject

  # provider usage 缺失时的结算策略：estimate | unknown | reject
  missing_usage: estimate
  # provider usage 超过预估时：observe | reject
  usage_mismatch: observe
  # 未配置模型价格时：observe | reject
  missing_price: observe
```

### 典型控制结果

```python
from agentguard import AgentGuard

guard = AgentGuard(
    max_output_tokens=1024,
    run_token_budget=8000,
    on_budget_exceeded="reject",
)

with guard.run(name="customer_support", user_id="user-123") as run:
    result = run.invoke(agent, "请帮我总结这份文档")
```

当 Agent 已经消耗 7,900 Token 时，AgentGuard 可以根据预估的下一次请求大小，在请求发出前拒绝、降级或停止当前 Run，而不是等到 Run 结束后才发现超过预算。

## 🧩 开源版与商业版

AgentGuard 的核心原则是：**实时决策能力开源，组织级治理能力商业化**。这样既方便个人开发者快速接入，也为团队和企业提供清晰的升级路径。

### 开源版（AgentGuard OSS）

开源版适合个人项目、实验环境和希望自托管的团队，建议使用 Apache License 2.0 发布：

- Python SDK、Wrapper、装饰器和主流框架集成；
- 本地 YAML 配置与策略执行；
- 单进程或单实例预算控制、限流、超时和熔断；
- 基础质量校验、自动重试、模型降级和行为异常检测；
- 本地日志、基础指标导出和自定义拦截器；
- 语义缓存适配器，以及 Chroma、Qdrant 等存储后端；
- 完整的插件接口、示例和本地测试工具。

### 商业版（AgentGuard Cloud / Enterprise，规划中）

商业版不替代开源 SDK，而是在其上提供集中式控制平面，适合多团队、多项目和生产级部署：

| 商业能力 | 解决的问题 |
| --- | --- |
| 组织与项目空间 | 统一管理团队、环境、应用和模型权限 |
| 中央预算与配额 | 跨进程、跨容器、跨地域同步 RPM、TPM、CPM 和每日预算 |
| 策略控制台 | 不改业务代码即可发布、灰度和回滚防护策略 |
| 实时 Dashboard | 查看成本、Token、延迟、质量、缓存命中率和错误率 |
| 审计与合规 | 记录策略变更、调用链路、人工审批和异常事件 |
| 告警与事件响应 | 对超支、质量下降、模型异常和工具滥用发送告警 |
| 评估工作台 | 管理评测集、回放请求并比较模型与 Prompt 版本 |
| SSO、RBAC 与企业支持 | 对接企业身份系统，细分管理员、开发者和审计员权限 |
| 托管部署与 SLA | 提供云服务、私有化部署、升级支持和可用性保障 |

### 能力边界

```text
业务应用
   │
   ├── AgentGuard OSS SDK       ← 本地实时拦截，低延迟、可离线、自托管
   │       ├── 预算 / 限流 / 熔断
   │       ├── 质量 / 重试 / 降级
   │       └── 日志 / 指标 / 插件
   │
   └── 商业控制平面（规划中）   ← 组织治理，集中配置、分析、审计
           ├── 策略中心与版本发布
           ├── 配额中心与成本分摊
           ├── Dashboard 与告警
           └── 评估、回放与合规
```

开源 SDK 即使不连接商业控制平面，也应保持核心防护能力完整可用；商业能力主要通过远程策略、指标聚合和治理服务增强，而不是给本地运行时增加强依赖。

## 🏗️ 推荐产品分层

为了让项目更容易演进，可以将能力拆成四层：

1. **Runtime Guard**：请求拦截、Token/成本预算、超时、熔断、重试和降级；
2. **Policy Engine**：将规则抽象为可组合的策略，支持 YAML、Python 和远程配置；
3. **Telemetry Layer**：统一采集 Trace、Metrics 和结构化事件，兼容 Prometheus、OpenTelemetry 等生态；
4. **Control Plane**：面向团队的策略发布、配额、告警、审计、评估和成本分析。

这种分层可以避免 SDK 变成“大而全的平台”，也能让社区贡献集中在稳定的扩展接口上。

## 🧠 工作原理

AgentGuard 采用“洋葱模型”，请求依次穿过各层策略拦截器：

```text
应用 / Agent
    ↓
AgentGuard Wrapper
    ↓
请求前拦截：预算 → 限流 → 缓存 → 请求校验
    ↓
模型或工具调用
    ↓
请求后拦截：质量评估 → 重试 / 降级 → 指标记录
    ↓
最终输出
```

每一层策略都可以独立配置，也可以通过自定义拦截器扩展。

### 一次 Agent Run 的生命周期

AgentGuard 建议将一次完整任务定义为 `Run`，将其中的模型、工具、检索和自定义步骤定义为 `Span`：

```text
Run: 用户的一次完整任务
├── Span: Agent 规划
│   └── LLM Call
├── Span: RAG 检索
│   ├── Embedding Call
│   └── Vector Search
├── Span: Tool Call
│   └── HTTP / Database / MCP
├── Span: Agent 反思
│   └── LLM Call
└── Span: 最终回答
    └── LLM Call
```

预算、耗时和质量既可以记录在单个 Span 上，也可以聚合到整个 Run。这样才能回答：一次业务请求为什么慢、为什么贵、在哪一步质量下降，以及是否发生了无效循环。

## ⏱️ 耗时治理

只记录 `total_latency` 很难指导优化。AgentGuard 应将一次 Agent 执行拆分为以下指标：

| 指标 | 含义 |
| --- | --- |
| `queue_latency_ms` | 请求等待限流器或并发槽位的时间 |
| `guard_latency_ms` | AgentGuard 自身执行规则和序列化的耗时 |
| `llm_ttft_ms` | 流式模型从请求发出到首个 Token 的时间 |
| `llm_generation_ms` | 模型持续生成内容的时间 |
| `tool_latency_ms` | 工具、HTTP、数据库或 MCP 调用耗时 |
| `retrieval_latency_ms` | Embedding、向量检索和重排耗时 |
| `retry_latency_ms` | 自动重试额外产生的等待时间 |
| `run_latency_ms` | 一次完整 Agent Run 的端到端耗时 |

在“记录”之外，还可以执行超时策略：

- 单次 LLM 调用超时；
- 单次工具调用超时；
- 整个 Agent Run 的 Deadline；
- 排队等待超时；
- 达到软超时后降级模型，达到硬超时后终止 Run；
- 对异常慢调用采样并保留完整 Trace。

## 🧠 质量治理

“质量”不是一个通用分数。不同应用需要不同评估器，AgentGuard 更适合提供统一接口和动作编排，而不是内置一个无法解释的万能评分。

### 可组合的质量指标

- **格式质量**：JSON Schema、字段完整性、类型和长度约束；
- **任务相关性**：回答是否围绕用户问题，是否遗漏关键要求；
- **事实一致性**：回答是否与检索证据、工具结果或业务数据一致；
- **引用完整性**：关键结论是否能映射到对应证据；
- **安全质量**：是否包含敏感数据、危险内容或越权信息；
- **工具执行质量**：工具参数是否合法，工具结果是否被正确使用；
- **业务规则质量**：例如客服必须包含工单号，SQL Agent 只允许只读查询。

### 质量不合格后的动作

```text
质量检查失败
   ├── retry      使用同一模型重新生成
   ├── repair     只修复格式，不重新执行完整 Agent
   ├── fallback   切换更强模型或备用工作流
   ├── human      进入人工审核队列
   ├── block      阻止不安全输出返回用户
   └── observe    仅记录，不影响线上结果
```

每种动作都必须继续受 Token、成本和重试次数限制，避免质量策略导致无限重试。

## 🚦 限流与并发治理

Agent 应用的限流不能只看 RPM。一次请求可能触发多次模型调用和工具调用，因此需要多个维度共同控制：

| 维度 | 示例 |
| --- | --- |
| 请求速率 | `30 requests/minute` |
| Token 速率 | `100000 tokens/minute` |
| 成本速率 | `$5/hour` 或 `$50/day` |
| 并发 Run | 同一租户最多运行 10 个 Agent 任务 |
| 并发模型调用 | 同一模型同时最多 5 个请求 |
| 工具调用频率 | 邮件工具每个用户每分钟最多调用 3 次 |
| 失败速率 | 连续错误达到阈值后触发熔断 |

限流键可以组合为 `organization/project/environment/agent/user/model/tool`。开源版可提供内存和 Redis 后端，实现单机或分布式原子扣减；商业控制平面则负责配置下发、全局配额和跨集群统计。

## 🚀 安装与开始

### 1. 安装

```bash
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
python3 -m pytest
```

以上命令在仓库根目录执行，需要 Python 3.10+。`requirements.txt` 安装 YAML、Redis
客户端和测试依赖；项目本身通过根目录的 `pyproject.toml` 以可编辑模式安装。
只安装核心 SDK 可执行 `python3 -m pip install -e .`；启用 Redis 后端还需独立运行
Redis 服务。语义缓存尚未实现，当前没有 `cache` extra。

### 2. 创建配置文件（可选）

在项目根目录创建 `agentguard.yaml`：

```yaml
token:
  max_output_tokens: 1024
  max_input_tokens: 12000
  run_token_budget: 8000
  reservation_buffer: 0.1
  retry_budget_ratio: 0.2
  on_exceeded: reject

budget:
  max_cost_usd: 0.05 # 每次调用的成本上限（美元）
  run_cost_usd: 0.50 # 一次 Agent Run 的累计成本上限

rate_limit:
  backend: memory # memory | redis
  redis_url: null # backend=redis 时必填
  redis_failure_policy: reject # reject | memory
  redis_prefix: agentguard
  redis_window_seconds: 60
  rpm: 30 # 每分钟最多 30 个请求
  tpm: 100000 # 每分钟最多 100,000 Token
  max_concurrency: 10 # 最大并发 Run 数
  max_tool_calls: null # 每个 scope key 的工具调用上限
  max_scope_keys: 10000 # 本地 admission 保存的最大 scope key 数
  wait_timeout_seconds: 0 # admission 最长等待时间；超时拒绝

quality:
  min_similarity: 0.8 # 低于该阈值时触发重试
  strict_json: false
  max_retries: 2

degradation:
  fallback_model: "gpt-3.5-turbo"
  enable_semantic_cache: true

timeout:
  llm_seconds: 60
  tool_seconds: 30
  run_seconds: 180
```

### 3. 保护现有代码

```python
from openai import OpenAI
from agentguard import AgentGuard

guard = AgentGuard(config_path="agentguard.yaml")
client = guard.wrap(OpenAI())

response = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "讲个 100 字笑话"}],
)
print(response.choices[0].message.content)
```

运行后，控制台会输出守护日志，例如：

```text
[AgentGuard] 🛡️ Budget: $0.01 / $0.05 used. Tokens: 120 / 2000.
[AgentGuard] ⏱️ LLM Latency: 1.2s | Tool Call: 0.4s | Total: 1.6s
[AgentGuard] ✅ Quality Score: 0.92 (Passed)
```

## ⚙️ 配置参考

| 顶级键 | 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `token` | `max_output_tokens` | `int` | `None` | 单次模型请求的输出 Token 硬上限 |
| `token` | `max_input_tokens` | `int` | `None` | 单次模型请求的输入 Token 上限 |
| `token` | `run_token_budget` | `int` | `None` | 一次 Agent Run 的累计 Token 硬预算 |
| `token` | `reservation_buffer` | `float` | `0.1` | 请求前预留的估算误差比例 |
| `token` | `retry_budget_ratio` | `float` | `0.2` | 重试可使用的独立预算比例 |
| `token` | `on_exceeded` | `str` | `reject` | 超预算动作：`reject`、`downgrade` 或 `stop_stream` |
| `budget` | `max_tokens` | `int` | `4096` | 兼容旧配置的单次 Token 上限 |
| `budget` | `max_cost_usd` | `float` | `None` | 单次调用的美元成本上限 |
| `budget` | `run_cost_usd` | `float` | `None` | 一次 Agent Run 的累计成本上限 |
| `budget` | `cost_warning_threshold` | `float` | `0.8` | 达到预算比例后打印警告 |
| `rate_limit` | `rpm` | `int` | `None` | 每分钟最大请求数 |
| `rate_limit` | `tpm` | `int` | `None` | 每分钟最大 Token 数 |
| `rate_limit` | `max_concurrency` | `int` | `None` | 最大并发 Run 数 |
| `rate_limit` | `max_tool_calls` | `int` | `None` | 每个 scope key 的工具调用上限 |
| `rate_limit` | `max_scope_keys` | `int` | `10000` | 本地 admission 最大 scope key 数，空闲 key 可被清理 |
| `rate_limit` | `wait_timeout_seconds` | `float` | `0` | admission 等待超时，超时后拒绝 |
| `rate_limit` | `backend` | `str` | `memory` | admission 后端：`memory` 或 `redis` |
| `rate_limit` | `redis_url` | `str` | `None` | Redis 后端连接地址 |
| `rate_limit` | `redis_failure_policy` | `str` | `reject` | Redis 不可用时拒绝或回退本地 admission |
| `rate_limit` | `redis_prefix` | `str` | `agentguard` | Redis key 前缀 |
| `rate_limit` | `redis_window_seconds` | `int` | `60` | Redis 滑动窗口时长 |

限流与熔断按配置的 scope key 隔离；本地 `AdmissionController` 同时管理 RPM、TPM 和并发
reservation，`CircuitRegistry` 为每个 scope key 独立维护 closed/open/half-open 状态。
上游异常会计入熔断，参数校验错误和预算/限流拒绝不会计入上游失败。

需要跨进程限流时，可选使用 `RedisAdmissionBackend`。它通过 Lua 原子脚本和 Redis 服务端
时钟完成 RPM/TPM reservation，并返回必须在调用结束时释放的 lease；核心安装不强制依赖
Redis 客户端，需由应用自行注入兼容 client。
| `rate_limit` | `cpd` | `float` | `None` | 每日最大成本（美元） |
| `quality` | `min_similarity` | `float` | `0.7` | 语义相似度阈值 |
| `quality` | `strict_json` | `bool` | `False` | 是否强制输出合法 JSON |
| `quality` | `retry_on_low_quality` | `bool` | `True` | 低质量时是否自动重试 |
| `quality` | `max_retries` | `int` | `2` | 最大重试次数 |
| `degradation` | `fallback_model` | `str` | `None` | 预算不足时使用的备用模型 |
| `degradation` | `enable_semantic_cache` | `bool` | `False` | 是否启用语义缓存 |
| `degradation` | `cache_similarity_threshold` | `float` | `0.9` | 语义缓存命中阈值 |
| `timeout` | `llm_seconds` | `float` | `None` | 单次 LLM 调用 deadline，包含流式消费 |
| `timeout` | `tool_seconds` | `float` | `None` | 单次工具调用 deadline |
| `timeout` | `run_seconds` | `float` | `None` | 一次 Agent Run 的总 deadline |
| `tools` | `max_calls` | `int` | `None` | 每个 Run 的总工具次数 |
| `tools` | `max_steps` | `int` | `None` | 每个 Run 的工具步骤上限；当前每次准入算一步 |
| `tools` | `max_repeated_calls` | `int` | `None` | 同工具和规范化参数的重复次数上限 |
| `tools` | `per_tool_limits` | `dict[str, int]` | `{}` | 按工具名设置 Run 内次数上限 |
| `tools` | `side_effects` | `dict[str, str]` | `{}` | `read/write/irreversible`，未标注默认为 write |
| `retry` | `max_retries` | `int` | `0` | 非流式模型额外尝试次数，0 关闭，最多 10 次 |
| `retry` | `initial_delay_seconds` | `float` | `0.5` | 指数退避的初始等待 |
| `retry` | `max_delay_seconds` | `float` | `10` | 退避上限，Retry-After 超过时停止 |
| `retry` | `max_elapsed_seconds` | `float` | `30` | 包含首次请求和等待的总时限，必须大于 0 |
| `retry` | `jitter` | `bool` | `True` | 在当前退避上限内随机等待 |
| `telemetry` | `verbose` | `bool` | `True` | 是否打印详细守护日志 |
| `telemetry` | `enable_metrics_export` | `bool` | `False` | 是否导出 Prometheus 指标 |

## 📐 指标与事件模型

AgentGuard 的数据输出应保持供应商无关，让 OpenAI、Anthropic、本地模型和不同 Agent 框架最终产生一致的记录。

### Run 摘要

```json
{
  "run_id": "run_01J...",
  "agent_name": "customer_support",
  "status": "completed",
  "started_at": "2026-09-03T10:00:00Z",
  "latency_ms": 2860,
  "input_tokens": 1820,
  "output_tokens": 640,
  "cached_tokens": 320,
  "total_tokens": 2460,
  "estimated_cost_usd": 0.0184,
  "llm_calls": 3,
  "tool_calls": 2,
  "retry_count": 1,
  "quality_score": 0.91,
  "budget_status": "within_limit"
}
```

### 关键事件

| 事件 | 触发时机 |
| --- | --- |
| `run.started` | 一次 Agent 任务开始 |
| `llm.requested` | 模型请求通过前置规则，即将发出 |
| `llm.completed` | 收到模型响应并完成 usage 结算 |
| `tool.started` / `tool.completed` | 工具调用开始或结束 |
| `budget.warning` | Token 或成本达到软阈值 |
| `budget.exceeded` | 硬预算耗尽，请求被阻止 |
| `rate_limit.waited` | 请求进入等待队列 |
| `rate_limit.rejected` | 请求因配额不足被拒绝 |
| `quality.failed` | 输出未通过质量评估 |
| `policy.degraded` | 模型、参数或工作流发生降级 |
| `loop.detected` | 检测到重复调用或无进展循环 |
| `run.completed` / `run.failed` | Run 正常完成或失败 |

### Token 统计口径

为了避免不同供应商字段不一致，建议统一保存：

- `input_tokens`：输入消息、系统 Prompt、工具定义等消耗；
- `output_tokens`：模型实际生成内容消耗；
- `cached_tokens`：供应商 Prompt Cache 命中的 Token；
- `reasoning_tokens`：供应商暴露时单独记录推理 Token；
- `estimated_tokens`：请求前通过 tokenizer 或近似算法获得的估算值；
- `billed_tokens`：供应商最终用于计费的 Token，若接口未返回则为空；
- `reserved_tokens`：并发控制阶段预留、结算后释放的 Token。

真实 usage 优先使用供应商返回值；供应商不返回时才使用本地 tokenizer 估算，并通过 `usage_source=provider|tokenizer|estimate` 标明数据来源。

## 🛠️ 自定义拦截器与评估器

如果内置规则无法满足需求，可以注入自定义逻辑。

### 自定义敏感词拦截器

```python
from openai import OpenAI
from agentguard import AgentGuard
from agentguard.interceptor import BaseInterceptor


class SensitiveWordInterceptor(BaseInterceptor):
    def before_request(self, messages):
        for message in messages:
            if "password" in message["content"].lower():
                raise ValueError("🚫 检测到敏感词，请求已拦截")


guard = AgentGuard()
guard.register_interceptor(SensitiveWordInterceptor())
client = guard.wrap(OpenAI())
```

### 替换质量评分器

```python
from agentguard import AgentGuard
from agentguard.quality import BaseQualityScorer


class MyCustomScorer(BaseQualityScorer):
    def score(self, prompt: str, output: str) -> float:
        # 可以调用模型作为裁判，也可以使用本地规则
        return 1.0 if len(output) > 50 else 0.0


guard = AgentGuard(quality_scorer=MyCustomScorer())
```

## 📊 性能基准

以下结果来自包含 2 次工具调用的标准 ReAct Agent 测试：

| 指标 | 无 AgentGuard | 仅启用限流 | 全功能 + 缓存 |
| --- | ---: | ---: | ---: |
| 平均延迟 | 8.2 秒 | 8.3 秒（+1%） | 4.5 秒（−45%） |
| Token 消耗 | 2,100 | 2,100 | 890（−58%） |
| 成本（GPT-4） | $0.084 | $0.084 | $0.022（−74%） |
| `429` 错误率 | 12% | 0% | 0% |

AgentGuard 自身的限流与质量检查开销小于 50 ms，具体结果会因模型、网络和业务负载而变化。

## 🎬 典型使用场景

| 场景 | 推荐能力 | 重点收益 |
| --- | --- | --- |
| 个人 Agent 或 Demo | 本地预算、超时和死循环检测 | 防止调试阶段意外产生高额费用 |
| RAG 问答系统 | 语义缓存、检索耗时追踪和答案质量检查 | 减少重复调用，定位检索与生成瓶颈 |
| 多租户 SaaS | 租户级预算、优先级队列和限流 | 避免单个租户占满共享配额 |
| 自动化办公 Agent | 工具白名单、敏感参数拦截和人工审批 | 降低误操作与越权调用风险 |
| 客服与知识库 | 评测集、低质量重试和模型降级 | 保证输出稳定并控制高峰成本 |
| 企业生产环境 | 集中策略、审计、告警和成本归因 | 支持上线后的持续治理与合规 |

## 🔐 安全与治理方向

AgentGuard 不应只关注“模型调用花了多少钱”，还可以逐步覆盖 Agent 的完整执行边界：

- **工具权限控制**：为每个 Agent 配置工具白名单、调用次数上限和参数约束；
- **Prompt 注入防护**：识别外部文档中的指令劫持、越权请求和系统 Prompt 泄露；
- **敏感信息保护**：对手机号、邮箱、身份证号、密钥和内部文档进行脱敏或阻断；
- **数据出境策略**：按租户、地域、数据等级限制可使用的模型与供应商；
- **人工审批节点**：涉及付款、删除、发信或生产变更时暂停 Agent，等待人工确认；
- **最小权限运行**：将“能调用什么工具”和“能读取什么数据”纳入策略，而不是只依赖 Prompt；
- **安全事件回放**：保存经过脱敏的决策轨迹，支持复盘、定位和策略改进。

这些能力建议以独立策略插件实现，避免把安全规则硬编码到某个模型或框架适配器中。

## 🗺️ 路线图

路线图按“先保证运行时控制，再建设治理平台”的顺序推进。已完成项仅覆盖描述的范围，不代表生产就绪或整个阶段完成；详细状态和门禁以 [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) 为准。

### 运行时守卫（开源优先）

- [x] Token 预估、预留、供应商 usage/估算结算和 Run 级硬预算（费用账单仍为估算）
- [x] 单次请求 `max_output_tokens` 注入与流式预算停止
- [x] 成本计算、版本化模型价格表和请求/Run 成本上限
- [x] RPM、TPM、并发数和工具调用限制（本地或 Redis 限流；租户级长期配额未实现）
- [x] 模型、工具、Run 耗时事件（RAG/缓存专用集成尚未实现）
- [x] 异步协作超时、同步检查点、熔断、能力约束模型降级、工具幂等/审批门禁和可选模型重试
- [x] JSON Schema、文本规则、可插拔质量评估器与受预算约束的非流式质量重试
- [ ] Agent 循环检测、最大步数和无进展检测

### Developer Experience（开源优先）

- [x] 通用客户端 Wrapper、装饰器和 Run 上下文管理器（不表示已通过所有供应商兼容测试）
- [ ] LangChain、LlamaIndex、CrewAI、AutoGen 和 MCP 适配器
- [x] 内存 admission/预算、Redis admission 与显式注入的共享预算、SQLite Run/策略/审计/账本存储；Redis 切主保证需部署验收
- [ ] Prometheus / OpenTelemetry 导出
- [ ] `agentguard init`、策略校验和本地模拟器
- [x] 单元/集成/浏览器端到端测试与发布 CI 配置；真实供应商兼容矩阵、托管运行与外部发布尚待验收

### 本地控制台（当前可运行）

- [x] Vue 3 运行概览、运行列表/调用树、策略编辑和接入设置
- [x] FastAPI + SQLite 事件查询、原子策略版本、单工作空间角色授权和签名远程策略
- [x] `npm.cmd --prefix frontend run test:e2e` 执行真实 HTTP 浏览器验证

### Control Plane（商业版或独立部署）

- [ ] 集中策略中心与版本发布
- [ ] 组织、项目、环境、Agent 和租户配额
- [ ] 成本归因、预算告警和策略审计
- [ ] Trace Dashboard、异常检索和请求回放
- [ ] 评测集、质量回归和模型 / Prompt 对比
- [ ] SSO、RBAC、私有化部署、备份和 SLA

## 🔭 可扩展方向

以下方向可以作为社区贡献、商业版能力或后续版本的候选池：

### 模型与供应商治理

- 多供应商路由：OpenAI、Anthropic、Google、本地模型和私有部署模型统一接入；
- 基于成本、延迟、质量和地域的动态路由；
- 供应商故障转移、健康检查和熔断恢复；
- 模型价格表版本化，支持历史成本重新计算；
- 对 Prompt、模型参数和输出进行版本绑定，便于结果对比。

### Agent 可观测性

- 完整 Trace：记录 Agent、LLM、工具、检索、缓存和重试之间的父子关系；
- Token、成本、延迟和质量的统一事件模型；
- 单次请求回放与“影子运行”（只评估、不产生真实副作用）；
- 按应用、租户、团队、模型和环境进行成本归因；
- 接入 OpenTelemetry、Prometheus、Grafana、DataDog 等生态。

### 评估与质量工程

- 管理离线评测集和线上采样集；
- 支持准确性、相关性、事实性、安全性和格式遵循等评分维度；
- 对 Prompt、模型、检索器和工具版本进行 A/B 测试；
- 质量回退时自动切换策略，并生成可定位的失败样本；
- 提供 CI 中的 Agent 回归测试和发布门禁。

### 开发者体验

- `agentguard init` 生成配置和最小接入示例；
- CLI 查看当前预算、策略和最近异常；
- 本地策略模拟器：在不调用真实模型的情况下测试拦截结果；
- 适配 FastAPI、Django、Celery、MCP 等常见运行环境；
- 通过 Python 客户端连接统一控制平面，保持开源 SDK 的单语言实现。

### 商业化路径建议

| 阶段 | 产品形态 | 主要用户 | 核心价值 |
| --- | --- | --- | --- |
| Phase 1 | 开源 SDK | 个人开发者与早期团队 | 快速接入实时防护 |
| Phase 2 | 托管控制台 | 多应用 SaaS 团队 | 集中策略、指标和预算管理 |
| Phase 3 | 企业版 | 大型组织与受监管行业 | 私有化、SSO、审计、合规和 SLA |
| Phase 4 | 生态平台 | 集成商与策略开发者 | 插件、评估模板和策略市场 |

商业版的收费点可以围绕“治理规模”而不是基础 SDK 功能设计，例如活跃 Agent 数量、月度 Trace 量、团队数量、私有部署支持和企业服务等级。

## 🤝 贡献指南

欢迎提交 Issue、Pull Request 以及其他形式的贡献。

贡献流程、本地开发环境和代码规范将在项目仓库稳定后补充到 `CONTRIBUTING.md`。

## 📄 许可证

AgentGuard 计划使用 Apache License 2.0 许可证。正式发布前，请在仓库根目录补充完整的 `LICENSE` 文件。

Made with ❤️ for the AI Agent community.
