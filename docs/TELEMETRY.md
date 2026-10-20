# 普通遥测与本地保留

事件 schema 为 v1。运行时为每个事件生成稳定 `event_id`，附带 UTC 时间、`run_id` 和 `trace_id`；调用事件还包含请求及父调用关联。终态、预算、安全、质量、缓存和重试沿用同一事件信封。存储与导出共同使用 `domain/events.py` 的有界标量白名单，不导出 messages、响应、工具参数、评估器说明或异常原文。模型名、工具名、关联 ID 等操作元数据也不得放入秘密。

```python
from agentguard import AgentGuard, JSONLEventSink

guard = AgentGuard(
    event_sink=JSONLEventSink(".agentguard/events.jsonl"),
    event_queue_capacity=1024,
)
try:
    with guard.run(name="example") as run:
        run.invoke(lambda: {"content": "ok"})
    assert guard.flush_events(timeout=2)
finally:
    drained = guard.close(timeout=5)
    # drained=False 表示仍有阻塞中的导出，应用应记录告警。
```

另有 `MemoryEventSink(max_events=1000)` 和 `CallbackEventSink(callback)`。传入 AgentGuard 的 sink 自动通过后台单线程有界队列调用；SDK 不同步等待导出。sink 异常被计数且不影响业务，失败不无限重试。队列满时丢弃最旧的待导出事件并增加 dropped；正在导出的事件不被回收。Memory sink 达到容量后滚动覆盖历史，它是查看窗口，不是归档。

`CallbackBatchEventSink(callback)` 支持一次接收多个已脱敏事件；`AgentGuard(event_batch_size=64)` 配置每批上限 1–1000。worker 直接取当前可用批次，不等待凑满。整批回调抛错时该批事件全部计入 failed，不自动重放；接收方可能已处理部分事件，应以 event_id 幂等。普通 sink 继续逐条调用，每条独立统计失败。

## 采样

```python
from agentguard import EventSampling, CallbackBatchEventSink

guard = AgentGuard(
    event_sink=CallbackBatchEventSink(your_bounded_batch_callback),
    event_batch_size=64,
    event_sampling=EventSampling(success_rate=0.1, slow_seconds=1,
                                tenant_rates={"your-tenant-id": 0.25}),
)
```

成功事件按 SDK trace_id 做确定性采样，普通请求链的事件使用相同选择。错误、拒绝、取消、安全发现/脱敏、未知提交、慢事件和 audit.* 保留导出资格；队列溢出或 sink 故障仍可能丢失普通事件，因此它们不是可靠审计。错误或慢请求的前置成功事件可能已被采样，不承诺完整尾部采样重建。无关联 ID 的事件保守保留。

最多配置 1000 个租户覆盖；租户身份只在进程内选择采样率，不自动写入导出事件或指标 label。每个 Run 捕获自己的租户；应用应让 tenant_id 和元数据 ID 不含秘密。采样只影响普通导出，不影响本地 Run 详情、SQLiteRunStore、预算或可靠 AuditStore。指标在采样之前计数；`sampled_out` 区分主动采样与 dropped，静止状态满足 received = exported + failed + dropped + sampled_out。设置页展示这两类计数。

## OpenTelemetry

安装 `.[otel]`；应用负责创建 TracerProvider、SpanProcessor 和有网络超时的 exporter：

```python
from agentguard import OpenTelemetryEventSink
from opentelemetry.sdk.trace import TracerProvider

provider = TracerProvider()
# 在应用启动时为 provider 注册 Simple/BatchSpanProcessor 与受控 exporter。
guard = AgentGuard(event_sink=OpenTelemetryEventSink(tracer_provider=provider))
try:
    with guard.run() as run:
        run.invoke(your_provider_call)
finally:
    guard.close()
    provider.force_flush()
    provider.shutdown()
```

适配器为 run/llm/tool 终态生成固定名称 span，绑定白名单元数据和已知耗时；失败设置 ERROR，不记录原始 exception、Prompt 或工具内容。模型/工具额外 timed_out 通知不重复生成 span。span 使用 OTel 自身 trace 标识，通过 agentguard.run_id/trace_id/request_id/parent_id 属性关联 SDK 调用关系；当前不声称重建原生 OTel 父子拓扑或传播供应商上下文。终态 span 无长期开放对象表，取消/丢事件不积累未关闭 span。Guard 不拥有应用的 provider/exporter，不自动创建网络连接，也不负责外部 collector 的部署和查询 UI。真正 SDK InMemorySpanExporter 契约在可选依赖测试中验证。

`flush_events` 等待队列与进行中的回调清空，`close` 停止接收并在指定时限内排空；两者 timeout 范围 0–60 秒。阻塞的自定义回调无法强行终止，close 返回 False 后 daemon 仍可能继续完成剩余事件。调用方应为回调的网络操作配置超时。该关闭流程只管理导出器，不关闭调用方拥有的 SQLite 等存储。

JSONL 默认每文件 10 MiB，保留两个轮转备份；支持配置容量和备份数。每行写完关闭文件，但不承诺 fsync、崩溃恢复或多进程共写同一路径的正确性。自行管理 JSONL 的时间保留和文件权限。

控制台接口：

| 接口 | 语义 |
| --- | --- |
| `GET /api/v1/telemetry` | 当前进程 received/exported/failed/dropped、队列、worker 和清理计数，重启归零 |
| `GET /api/v1/metrics` | 默认 404；设置 `telemetry.enable_metrics_export=true` 后输出 Prometheus 文本 |

指标包含有界事件 family、模型调用成功/失败、Token 与估算成本、重试、缓存命中/未命中、安全/质量/预算拒绝，以及固定桶的模型耗时和首片段耗时直方图。family 是固定集合；不以模型名、租户、用户输入或请求 ID 作为 label。指标在入队时更新，导出丢失不减少已观察计数。首片段时间是 SDK 收到第一个 chunk 的时间，包括可能仅含元数据的 chunk，不保证与供应商首个文本 Token 时间相同。成本是 SDK 估算，不是权威账单。

模型缓存使用 `agentguard_cache_hits_total` / `agentguard_cache_misses_total`，只读工具缓存独立使用 `agentguard_tool_cache_hits_total` / `agentguard_tool_cache_misses_total`，不混计供应商用量。工具命中仍产生 tool.started/completed/finished 与审计记录，缓存事件为 cache.tool_miss/stored/hit/bypassed；事件只包含元数据。

`telemetry.retention_days` 默认 7，支持 1–30。HTTP 服务启动后每 60 秒清理同一数据库中已结束、结束时间超过保留期的运行及其事件，保留运行中的记录。缩短保留期会删除历史数据。独立 SDK 用户需自行调度 `SQLiteRunStore.prune(...)`；SQLite 文件不会自动 VACUUM，数据库文件实际占用和备份另行管理。

上述能力均为普通遥测。必需审计另行实现 SQLite FULL outbox、先落盘后执行、稳定 ID 与幂等归档、默认 180 天逻辑保留；磁盘持久性和备份删除仍依赖部署验收，见 [运维文档](OPERATIONS.md)。不能将普通遥测队列或 SQLite 摘要当作可靠审计或权威计费账本。批量回调和 OTel 可接应用自己的中心收集端；托管 collector、统一查询以及完整尾部采样仍未包含。
