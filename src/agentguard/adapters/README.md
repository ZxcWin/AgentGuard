# Adapters

放置 Port 的具体实现以及第三方集成：内存/SQLite/Redis、文件/Prometheus/OpenTelemetry、模型供应商和 Agent 框架适配器。

适配器必须可选安装、薄、可替换；不能在这里复制预算、限流或重试规则。
## 存储适配器

`adapters.storage.RedisAdmissionBackend` 不强制安装 `redis` 第三方包，调用方通过
兼容 `eval(script, numkeys, *keys_and_args)` 的 client 注入。预留请求使用单次 Lua
脚本原子完成 RPM、TPM 检查和写入，使用 Redis 服务端 `TIME`，避免应用进程时钟偏差。

```python
from agentguard import RedisAdmissionBackend
from redis import Redis

admission = RedisAdmissionBackend(Redis.from_url("redis://localhost/0"))
lease = admission.acquire(key="tenant=acme|model=demo", tokens=120, rpm=30, tpm=100_000)
try:
    ...
finally:
    lease.release()
```

Redis adapter 只负责跨进程 admission reservation；预算账本、事件 sink 和供应商调用
仍由上层组件负责。Redis 不可用时不会静默放行，调用方应捕获 client 异常并按业务的
fail-closed 策略处理。

公共运行时可通过 `rate_limit.backend: redis` 自动创建该 adapter；需要安装兼容的
Redis Python client。`redis_failure_policy: reject` 是默认安全行为，`memory` 只保留
本地并发和工具次数限制，不保证跨进程 RPM/TPM 一致性。
