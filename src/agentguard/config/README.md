# Config

配置代码按职责拆分：

- `schema.py`：配置 dataclass、schema version、类型和范围校验；
- `merge.py`：不修改输入的递归配置合并；
- `env.py`：`AGENTGUARD_` 环境变量解析与嵌套覆盖；
- `loader.py`：YAML/Mapping 读取、来源组合和最终配置构建。

价格配置示例：

```yaml
pricing:
  version: "2026-01"
  models:
    demo:
      input_per_million: 2.0
      output_per_million: 4.0
      cached_input_per_million: 0.5
```

配置读取失败必须显式报错；运行中的 Run 使用固定快照，不因热更新产生前后不一致。

预算结算异常策略位于 `token` 配置下：

```yaml
token:
  # provider 没有返回有效 usage 时使用保守估算；也可设为 unknown 或 reject
  missing_usage: estimate
  # provider usage 超过请求前预估时仅记录事件；也可设为 reject
  usage_mismatch: observe
  # 未配置模型价格时仅记录事件；也可设为 reject
  missing_price: observe
  # retry、quality_retry 和 fallback 共用父 Run 预算的独立比例
  retry_budget_ratio: 0.2
```

重试类 reservation 必须通过 `BudgetManager.reserve(..., purpose="retry")`（或
`quality_retry`、`fallback`）申请；它们同时受父 Run 硬预算和独立重试预算约束。

预算动作设为 `downgrade` 时，需配置 `degradation.fallback_model`。只有因父 Run、单次
请求 token/cost 硬预算拒绝时才会切换，并记录原模型、备用模型和拒绝原因；备用模型再次
无法预留预算时仍抛出原始 `BudgetExceeded`。流式请求会把请求前预估限制在剩余预算内，
消费过程中超出硬上限时停止迭代，并对已消费部分使用估算 usage 结算。
