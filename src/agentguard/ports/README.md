# Ports

放置 `typing.Protocol` 接口和跨层数据边界，例如 Tokenizer、Clock、Cache、BudgetStore、RateLimitStore、EventSink 和 PolicyProvider。

Port 不绑定具体实现，不导入供应商 SDK；具体实现放在 `adapters`。

本地 admission 目前由 `policies.rate_limit.AdmissionController` 提供：它按 scope key
组合 RPM、TPM 和并发限制，并返回必须释放的 `AdmissionLease`。等待使用单调时钟，
超时抛出 `RateLimitExceeded`；调用链会在成功、异常、取消和流式关闭时释放 lease。

`CircuitRegistry` 为每个 scope key 创建独立的 `CircuitBreaker`，支持 closed/open/half-open
和单探测并发。运行时默认使用租户、项目、环境、Agent、用户、模型和工具配置合并后的
scope key；上游异常计入失败，参数错误和 AgentGuard 自身拒绝不会污染上游熔断统计。

`RateLimitConfig.max_tool_calls` 会把工具调用次数纳入同一个 scope key 的 admission。
`max_scope_keys` 防止本地状态因高基数 key 无限增长；创建新 key 时优先淘汰最久未使用且
无活跃 lease 的 key，若所有 key 都活跃则拒绝新请求。
