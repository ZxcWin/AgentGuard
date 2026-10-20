# 审计、远程策略、共享预算和成本账本

本版提供单工作空间控制台与可注入的 SDK 组件。默认运行仍是本地开发模式；同一数据库是一套工作空间权限边界。组织管理、SSO、多租户行级授权、跨地域预算和商业结算不包含在本版中。

## 一起启动前后端

先安装 `.[console,console-test,yaml]`，在 frontend 执行 `npm ci`。项目根目录：

```powershell
.\.venv\Scripts\python.exe tools/dev.py --demo --host 192.168.3.185
```

启动 http://192.168.3.185:5173/，后端监听 127.0.0.1:8000。IP 使用实际本机地址；省略 host 则仅本机访问。Ctrl+C 同时停止两个进程。已有服务占用端口会直接提示，不会终止未知进程。演示模式不调用外部供应商。真实数据模式去掉 demo。

静态部署先运行 `npm --prefix frontend run build`，再启动 `python -m agentguard.api.http`，直接访问 8000。Vite 是开发服务，不用于公网部署。

可选同机持久缓存使用 `--cache-database .agentguard/cache.db --cache-key-file .agentguard/cache.key`，策略默认仍关闭。密钥由部署程序排他创建，32–4096 字节，按服务账户配置 ACL；使用独立数据库与独立用途密钥。重启复用、共享清空、容量和时钟处理见 [CACHE.md](CACHE.md)。质量修复与人工回调见 [QUALITY.md](QUALITY.md)，非流式结构化脱敏见 [CONTENT_SECURITY.md](CONTENT_SECURITY.md)。

## 身份与权限

```powershell
python tools/create_console_credentials.py
python -m agentguard.api.http --credentials .agentguard/credentials.json
```

生成工具以排他创建方式保存散列凭证与单独的 `.tokens.json`，不会覆盖旧文件或将令牌打印到终端。分别分发 viewer/operator/admin 凭证；Windows 使用部署账号的用户级 ACL，生产目录由服务账号独占。轮换时生成新文件并重启服务，旧令牌不再接受。

| 角色 | 允许操作 |
| --- | --- |
| viewer | 运行、概览、当前策略等普通只读查询 |
| operator | viewer 能力、策略校验、显式开启的演示操作 |
| admin | 发布策略、清缓存、审计查询/核对/恢复、账本读写 |

认证和授权由服务端逐请求执行。前端令牌只存在内存，刷新需重新连接；不放入 URL、localStorage 或构建变量。健康和 OpenAPI 描述公开，实际业务接口需要 Bearer。Swagger 的 Authorize 可以输入令牌。

远程绑定必须同时提供 credentials 与准确的 public-host，例如 `--host 0.0.0.0 --public-host console.example`。部署在 HTTPS 反向代理后，代理保留准确 Host，外部只暴露 HTTPS；不要在公网明文发送令牌。跨源网页另外传入准确 `--allow-origin https://console.example`，CORS 支持 Authorization 预检。默认不启用任意来源、Cookie 会话或远程匿名写入。TLS、速率保护、服务进程管理、磁盘加密和备份由部署环境提供。

## 必需审计与归档

```python
from agentguard import AgentGuard, SQLiteAuditStore

audit = SQLiteAuditStore(".agentguard/app.audit.db")
guard = AgentGuard(audit_store=audit, audit_required=True)
try:
    with guard.run() as run:
        run.invoke_tool("write", lambda: "completed")
finally:
    guard.close()
    audit.close()
```

控制台默认使用独立 `<数据库名>.audit.db`。模型/工具执行和策略/财务变更分别记录 intent/result；审批与安全/预算拒绝记录 decision。原文、工具参数、令牌和异常正文不进入审计。调用方提供的标识符仍需使用业务 ID，不能塞入秘密。

SQLite 使用 WAL、synchronous FULL、默认 2 秒锁等待。outbox 默认逻辑有效载荷上限 256 MiB、保留 180 天；数据库页、索引和 WAL 额外占磁盘，应另设磁盘配额及告警。写满或不可写会锁定后续受保护操作；最终结果落盘失败返回 AuditUnavailable，并提供 operation_id 和 execution_may_have_completed。已执行操作不会自动重放，已产生用量不会退款。

重启检测到未闭合 intent 时保持暂停。在审计页核对供应商/业务系统后记录完成、未执行或仍不确定，再显式恢复；“仍不确定”表示由管理员接管，不表示未执行。不要在原操作尚在执行时核对关闭；共享一个 outbox 的其他进程也需协调停机与核对。存储不提供外部副作用的原子事务，也不能阻止管理员直接改数据库。

```shell
python -m agentguard audit-deliver --source .agentguard/app.audit.db --archive .agentguard/archive.db --limit 100
```

命令每次最多处理 1000 条，可由调度器周期运行。目标 SQLite FULL 同步提交后才确认源记录；崩溃重投按 event_id 去重，冲突拒绝。源记录满 180 天且已投递、操作已闭合后才清理；未投递或未闭合记录不自动删除。归档按到达时间保留 180 天，命令运行时清理，确保至少保留该时长；备份仍须执行部署方的到期删除策略。SQLite 删除释放可复用页，不等同于物理安全擦除；备份和静态加密必须单独管理。

恢复与预算对账需要管理员核验真实结果。审计文件丢失应从可信备份恢复并核对，不能删库解除暂停。普通遥测丢弃策略不会作用于必需审计。

## Redis 共享预算

```python
from agentguard import AgentGuard, RedisBudgetStore

shared = RedisBudgetStore.from_url(
    "redis://127.0.0.1:6379/0", scope="tenant/project/prod/2026-10", epoch="epoch-1",
)
# 仅首次由配额管理员执行；已有作用域不能覆盖或归零。
shared.provision(token_limit=1000000, cost_limit_usd="20")
guard = AgentGuard(shared_budget_store=shared)
```

其他进程使用同一 scope/epoch 创建 store，但不调用 provision。SDK 先预留本地 Run 额度，再原子预留共享额度；拒绝会释放本地占用。相同 ID 与金额可幂等预留/结算；金额按整数微美元向上舍入。一个作用域的哈希键在同一 Cluster slot，记录数量有界，耗尽后拒绝。当前提供单作用域总额，未提供多层租户/模型额度的联合事务。

连接故障、未知提交、epoch 不匹配均拒绝新增调用，绝不退化为本地放行。状态无 TTL；已启动且结果未知的调用保留全额占用。未知预留提交会在 BudgetExceeded.reservation_id 与 budget.shared_uncertain 事件留下原 ID；不能换 ID 重新扣款。用 `status(reservation_id=...)` 查询，先 `pause()`，从真实 usage 确认后用 `reconcile(..., expected_tokens=..., expected_cost_usd=..., actual_tokens=..., actual_cost_usd=..., audit_store=audit, actor="admin")` 原子比较并结算。人工确认未执行时实际值填 0。对账只改额度记录，不重放请求。

修复后 `resume(audit_store=audit, actor="admin")` 需要管理员确认单写和持久性仍成立。Redis Lua 原子性不构成异步复制故障切换的强持久保证；部署需持久化、主节点 fencing、备份与故障切换暂停联动。客户端不能自动证明切主未丢已确认额度。未完成真实故障演练前，不标记跨实例预算生产就绪。

## 签名远程策略

SDK 通过 `HTTPPolicyProvider` 获取 HTTPS Bearer 认证的快照，用 `PolicyVerifier` 校验 HMAC-SHA256、作用域与版本，通过 `RemoteConfigManager` 提供给 AgentGuard。HMAC 是可信部署之间的共享秘密验证，持有验证密钥的节点也能签名；不提供公开分发的非对称签名隔离。

```python
from pathlib import Path
from agentguard import AgentGuard, RemoteConfigManager, PolicyVerifier, HTTPPolicyProvider

scope = {"tenant_id":"tenant", "project_id":"project", "environment":"prod"}
manager = RemoteConfigManager(
    HTTPPolicyProvider("https://console.example/api/v1/policies/snapshot", token="runtime-bearer-token"),
    PolicyVerifier({"primary": Path("policy.key").read_bytes()}),
    scope=scope, cache_path=".agentguard/policy-cache.json",
)
guard = AgentGuard(config_manager=manager)
with guard.run(**scope) as run:
    pass
```

示例令牌需由部署秘密管理提供，不能把占位值用于生产。服务端传入 `--policy-key-file`（至少 32 字节随机原始密钥）与 `--policy-scope`（scope JSON 文件），同时启用 credentials。签发接口默认 15 分钟有效期，不下发 Redis 凭证。密钥通过独立安全通道部署，验证器可配置多个 key_id，默认服务签发 primary。

刷新在新建 Run 时按默认 60 秒间隔触发，带 0.9–1.0 倍抖动，不启动后台轮询。断网可使用尚未过期且已验证的快照；过期拒绝新 Run 和旧 Run 的下一次模型/工具准入。等待和拦截器之后、实际调用之前复查。已执行请求允许结算与清理。快照同时使用 UTC 截止时间与单调计时，检测时钟明显回拨后保持暂停，需修复时钟并重启。

缓存以临时文件 fsync+replace 保存已签名快照和版本高水位；每个 SDK 实例使用独立 cache_path。损坏缓存拒绝启动，不能删文件绕过版本防回退。操作系统、目录权限和恢复流程须保证高水位不可被回滚。

在线回滚将旧内容重新发布为更高版本。离线恢复通过 `sign_policy(...recovery=True)` 生成独立高版本包，再 `manager.install_recovery(envelope, audit_store=audit, actor="admin")`；恢复有效期限制为 60–300 秒，只允许保留配置或降低明确列出的预算/次数/并发/超时上限，其他字段须保持相同。不能取消限制或换价，不能作为离线放权入口。不提供额外 fail-open 宽限。

## 成本账本

控制台成本账本页提供估算同步、账单 JSON 导入、不可变分录、正负调整和请求关联差异。数据库为独立 `<数据库名>.ledger.db`，服务端 Decimal 计算权威汇总。

明细格式为 `[{"line_id":"1","request_id":"req_...","amount_usd":"0.001"}]`，request_id 可省略。币种当前仅 USD，金额最多 9 位小数。同一 supplier/invoice/line 相同内容重复导入安全幂等，不同金额拒绝，修正需以唯一 adjustment_id 追加调整。导入整批事务原子；已提交而 HTTP 响应丢失时可以原样重试。

同步估算读取最近 1000 个终态 Run 的已记录调用，幂等保存；需要更长周期应在普通记录清理前周期同步。未匹配和未开票单独显示，不当作零成本；费用最终来源是管理员导入的供应商账单，当前不自动连接供应商账单 API，也不验证文件来源。多币种、税务、折扣分摊和商业收费不在本版范围。

## 备份、升级与验收

停下写入进程后备份运行/策略、审计、账本与策略高水位文件；在线备份使用 SQLite backup API，不能只复制正在使用 WAL 的主文件。审计、账本和 epoch 恢复必须一起核对，不可只恢复某个文件然后继续放行。新增表采用兼容创建；升级前备份，回滚代码前检查数据库兼容性。

本地验收命令见 RELEASE.md。真实 Redis 测试由 `AGENTGUARD_TEST_REDIS_URL` 指向隔离服务，不能用生产库测试。CI 服务仅删除唯一测试键，不 FLUSHDB。依赖审计、目标平台、真实供应商及 Redis 主备故障切换验收各自记录，不能用 fake Redis 或 CI 配置代替实际通过证据。
