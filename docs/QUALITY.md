# 质量失败后的恢复

`quality.action` 支持 `block`、`observe`、`retry`、`repair`、`degrade`、`human`，默认仍是阻断。恢复只用于非流式模型响应；每个新请求重新经过安全、准入、预算、审计和质量检查。失败响应已经产生的 usage 先结算，不返还费用。

所有恢复共用 `quality.max_retries`（默认 0，最多 10）、父 Run 硬预算、`token.retry_budget_ratio` 和重试总 deadline。`retry` 复用请求；`repair` 由应用构造新请求；`degrade` 使用同一供应商入口的备用模型，最多切换一次；`human` 由可信应用审批一个新请求，最多一次。流式、已经执行子工具的尝试和评估器异常均不自动重放。

## 修复请求

```python
from agentguard import AgentGuard, QualityRecovery, QualityRequest

def repair(failure):
    # failure.response 只交给这个显式注册的应用回调，不写日志/审计。
    return QualityRequest(
        args=failure.request.args,
        kwargs={**failure.request.kwargs, "messages": "请返回合法 JSON 对象"},
    )

guard = AgentGuard(
    {"quality": {"enabled": True, "strict_json": True,
                 "action": "repair", "max_retries": 1}},
    quality_recovery=QualityRecovery(repair=repair),
)
```

回调只能构造请求或等待应用审批，不能在同一 Run 内递归调用模型/工具。返回值不是可信响应；SDK 使用原供应商函数重新执行返回的 `QualityRequest`。回调不能更换模型、改为流式或修改 SDK 预算参数。需要更换模型时使用 `degrade`，并配置 `degradation.fallback_model/model_profiles` 及显式价格。能力、路由地区、数据等级和成本限制会在选择时及请求拦截器之后检查。

## 人工决定

```python
from agentguard import QualityDecision, SQLiteAuditStore

audit = SQLiteAuditStore(".agentguard/quality.audit.db")

def review(failure):
    # 由应用验证审批人的身份与权限，不从 Prompt 或模型输出读取 actor。
    return QualityDecision(
        approved=True, actor_id="trusted-reviewer-42",
        request=QualityRequest(args=failure.request.args,
                               kwargs={**failure.request.kwargs, "messages": "经审核的新请求"}),
    )

guard = AgentGuard(
    {"quality": {"enabled": True, "strict_json": True, "action": "human",
                 "max_retries": 1, "handler_timeout_seconds": 30}},
    audit_store=audit, quality_recovery=QualityRecovery(human=review),
)
```

人工模式必须注入可靠审计和审批回调；缺少任一项时拒绝。审批人标识、批准/拒绝、意图和结果持久记录，原响应、修复请求和评语不进入事件。拒绝、非法返回、回调异常、超时、取消或审计故障都不会触发第二次模型调用。批准不能跳过新请求的质量门禁。

`handler_timeout_seconds` 默认 30 秒，范围 `(0, 300]`，继承更短的 Run/LLM/重试 deadline。异步模型入口支持同步或异步回调；同步模型入口要求同步回调。同步回调执行前后检查时间，无法强制中断阻塞线程；异步回调采用协作取消。应用应让审批服务自身配置超时。此功能提供应用回调契约，不包含浏览器审批队列、跨实例待办或 SSO。

控制台可以保存所有动作、次数和时限；修复与人工回调由实际运行 SDK 的应用注册，不通过策略 JSON 分发可执行代码。完整离线演示见 [quality_recovery.py](../examples/quality_recovery.py)。
