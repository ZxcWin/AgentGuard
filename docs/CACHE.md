# 精确缓存与 SQLite 持久复用

默认缓存关闭，默认后端是进程内 MemoryExactCache。精确模型缓存仍要求模型白名单、显式纯调用、public/internal 等级、完整 Run 身份以及当前授权版本；命中重新执行安全与质量检查。凭据、会话、审批、个人信息、安全发现、错误/未完成响应、模型工具调用、流式、自动重试和带拦截器的调用不缓存。

## 明确授权的只读工具

仅当缓存策略同时启用、工具名列在 `cache.allowed_tools`、`tools.side_effects[name]` 明确设为 `read`，且调用提供 `ToolCacheRequest` 时才会读写工具缓存。请求还需设置 `pure=True` 和 `public`/`internal` 数据等级，并绑定 `tool_version`、`data_version`、授权版本 `authorization_context`、命名空间和完整 Run 身份。工具实现版本用于结果/算法变更；数据版本由应用随底层数据快照或索引版本递增。SDK 无法验证这些声明是否真实。

```python
from agentguard import AgentGuard, ToolCacheRequest

guard = AgentGuard({"cache": {"enabled": True, "allowed_tools": ["catalog_lookup"]},
                    "tools": {"side_effects": {"catalog_lookup": "read"}}})
options = ToolCacheRequest(namespace="product-catalog", prompt_version="n/a",
    authorization_context="catalog-reader-v4", data_classification="internal", pure=True,
    tool_version="search-algorithm-v2", data_version="catalog-snapshot-2026-10-19")
with guard.run(tenant_id="acme", project_id="shop", environment="prod", user_id="u-17") as run:
    result = run.invoke_tool("catalog_lookup", lookup, "item-42", cache_options=options)
```

每次命中仍执行审批/幂等预检、工具重复与额度规则、并发/速率准入、熔断检查、运行行为限制和持久审计；因此命中仍计为一次本地工具调用，但不会重新查询工具数据源。必须审批或要求幂等键的工具不会进入缓存，写入/不可逆工具始终拒绝。参数和结果经过有界 JSON、凭据/PII 与工具调用内容过滤；只保存被签名的结果信封。应用必须在权限、版本、数据可见范围变化时更新授权/数据版本，且不得把过期权限映射到同一版本。

工具缓存与模型缓存共用同一 store、TTL、容量、清空和签名策略。跨重启复用还需 SQLite、稳定签名密钥、显式 provider_id 及相同的全部身份/策略/版本。并发未命中可能重复执行只读查询；缓存不承诺 exactly-once，命中也不省掉本地配额与审计成本。语义匹配和网络共享缓存仍未实现。

## 同机持久后端

```python
from pathlib import Path
from agentguard import AgentGuard, SQLiteExactCache, CacheRequest

# 从服务账户独占的秘密文件读取；不同用途使用独立密钥。
key = Path(".agentguard/cache.key").read_bytes()
store = SQLiteExactCache(".agentguard/cache.db", max_entries=1000, max_bytes=16*1024*1024)
guard = AgentGuard(
    {"cache": {"enabled": True, "allowed_models": ["your-model"]}},
    cache_store=store, cache_signing_key=key,
)
options = CacheRequest(namespace="public-answers", prompt_version="v1",
    authorization_context="reader-permissions-v3", data_classification="public", pure=True,
    provider_id="your-provider/endpoint/deployment/revision-v1")
try:
    with guard.run(tenant_id="t", project_id="p", environment="prod", user_id="u") as run:
        result = run.invoke(your_provider_call, model="your-model", messages="hello", cache_options=options)
finally:
    guard.close()
    store.close()
```

跨实例/重启复用须同时满足：同一 SQLite 文件、32–4096 字节签名密钥、明确相同的 `provider_id`，以及全部请求/身份/策略版本一致。SDK 不推断供应商地址与部署身份；应用负责让 provider_id 唯一绑定到兼容入口和响应版本。不同入口不应复用同一 ID。省略稳定密钥或 provider_id 时使用进程随机身份，保守地不跨重启命中。

键与响应信封通过 HMAC 保护，绑定身份、请求参数、模型、部署、授权/Prompt/完整策略及版本；不保存原始请求。持久信封包含签名后的 Unix 签发/到期时间，TTL 最多一小时，命中同时验证后端与信封。SQLite 持久保存已观察时钟高水位，检测回退后使缓存操作失败；SDK 按未命中继续正常准入/预算流程。密钥不写进配置、数据库或事件，轮换密钥会隔离旧条目。

SQLite 使用 WAL、本地锁及 `BEGIN IMMEDIATE` 完成容量/淘汰/租约变更。构造器容量是后端上界，单次写入采用不超过该上界的策略限制。LRU、过期删除、逻辑字节/条目上限和显式 prune 有效；文件/WAL/备份的实际占用另外管理。支持同机不同连接/进程，不支持 NFS/网络共享盘。50ms 锁等待超限、读写/校验失败均降为缓存未命中。

共享租约有随机 owner 和 30 秒有效期，过期 owner 不能释放新 owner。缓存等待最多一秒，超时后正常请求；长请求或实例故障可能产生重复的纯模型调用，缓存不提供业务 exactly-once。`guard.clear_cache()` 删除条目并原子变更共享 generation，各连接及清空前的在途结果均不能重新命中旧代条目。跨线程清空与请求的语义以最后一次 generation 检查为边界，已经返回的数据不能撤回。

缓存 payload 是普通 JSON，不加密。部署者负责文件 ACL、磁盘加密、备份与删除；签名不提供保密性。只有明确允许缓存的公开/内部数据可以落盘。各服务关闭自己创建的 store，Guard.close 不关闭调用者注入的资源。完整可运行示例见 [persistent_cache.py](../examples/persistent_cache.py)。

## 控制台

先由部署程序使用排他创建方式生成 32 字节随机 cache.key，限制为服务账户读取，再启动：

```shell
python -m agentguard.api.http --cache-database .agentguard/cache.db --cache-key-file .agentguard/cache.key
```

两个参数必须同时提供，缓存数据库须独立于运行/审计/账本。同一工作空间的实例使用同一路径和密钥；不同工作空间不要共享缓存文件。策略默认仍关闭。前端显示实际 memory/SQLite 后端，管理员清空会产生可靠审计，并返回 local_process/shared_host 作用范围。其他 SDK 进程需自行注入对应 store 和密钥，不会因控制台启动参数自动启用。

当前不包含网络 Redis 缓存或向量/语义匹配。只读工具缓存的接口、限制、重启复用和本地演示见上文及 [readonly_tool_cache.py](../examples/readonly_tool_cache.py)。
