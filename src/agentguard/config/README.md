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

`ConfigManager(policy_store=SQLitePolicyStore(path))` 可显式使用本地持久策略库。
首次写入初始化版本，之后以数据库中的策略为准；每次创建 Run 读取快照。
版本更新在 SQLite 事务内比较预期版本，写入失败不发布新快照。
这只支持显式连接同一本地数据库的进程，不是网络策略下发。

工具/超时配置示例（默认全部不限制；0 表示禁止）：

```yaml
timeout:
  run_seconds: 60
  llm_seconds: 20
  tool_seconds: 5
tools:
  max_calls: 10
  max_steps: 10
  max_repeated_calls: 2
  per_tool_limits:
    lookup: 3
  side_effects:
    lookup: read
    send: irreversible
```

工具重复检查、副作用、协作取消与同步时限的完整语义见 `docs/RUNTIME.md`。

`retry.max_retries` 默认 0 关闭，范围 0–10；`initial_delay_seconds` 默认 0.5，
`max_delay_seconds` 默认 10，`max_elapsed_seconds` 默认 30，`jitter` 默认 true。
总时限必须大于 0，初始退避不得超过最大退避；次数与时间字段都不能为 null。
重试只覆盖非流式模型调用，工具/流不重试。每次重试使用 `purpose="retry"`，
受 `token.retry_budget_ratio` 与父 Run 硬预算约束，失败的未知 usage 保守计入估算预算。

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

限流后端默认是 `memory`。配置 `backend: redis` 时必须提供 `redis_url`；Redis 连接或
脚本失败默认按 `redis_failure_policy: reject` 拒绝请求，也可显式设为 `memory` 回退到
本地 admission。回退会保留本地并发限制，但不再声称具备跨进程 RPM/TPM 一致性。

预算动作设为 `downgrade` 时，需配置 `degradation.fallback_model`。只有因父 Run、单次
请求 token/cost 硬预算拒绝时才会切换，并记录原模型、备用模型和拒绝原因；备用模型再次
无法预留预算时仍抛出原始 `BudgetExceeded`。流式请求会把请求前预估限制在剩余预算内，
消费过程中超出硬上限时停止迭代，并对已消费部分使用估算 usage 结算。
