# 发布与兼容

当前包版本 0.1.0，为 unreleased。Python 核心包不包含前端构建产物，控制台源码与 Node 锁文件在 frontend 独立维护；部署前需先构建静态页面。API 主版本为 1，配置/schema 版本为 1，事件 schema 为 1；这些版本各自演进，不互相替代。

核心公共 API 为 `agentguard` 顶层导出、文档中的配置字段与 facade；application 内部辅助类不承诺稳定。0.x 的不兼容变更通过 minor 版本发布并在 CHANGELOG 说明；patch 只作兼容修复。已公开 API 计划移除时提前至少 90 天公告并提供替代方式，安全漏洞修复可例外，必须解释迁移。

已有本地数据库启动时通过缺省值补足新增字段，旧 Run 使用原快照；新策略仍采用更高版本和 expected_version 比较。Schema 类型和事件信封新增字段为兼容扩展，消费者应忽略未知字段；删除/改变既有字段语义需要升级相应主版本。修改配置 schema 或数据库结构前必须提供迁移与回滚说明；默认不尝试降级数据库。

本次升级：quality 新增 json_schema/min_chars/required_substrings/max_retries/handler_timeout_seconds，action 支持 retry/repair/degrade/human。默认仍关闭，次数为 0；修复/人工需应用注册回调，人工需可靠审计。security 新增非流式 redact，严格生产模式仍要求 block。新增 SQLiteExactCache、可选 cache_signing_key 和 CacheRequest.provider_id，稳定复用是显式配置；旧内存缓存会自然失效，不迁移原条目。使用 Schema 时安装 `[quality]`；使用 `[console]` 会同时安装 Schema 支持。telemetry 新增 retention_days，HTTP 服务默认每分钟删除超过 7 天的已结束记录；需要保留旧数据时，在升级前备份并调整 1–30 天的保留期。必需审计不能依赖这些记录。

2026-10-19 新增 `ToolCacheRequest`、`invoke_tool(cache_options=...)` 和 `cache.allowed_tools`（默认空名单）。只缓存显式版本化的纯只读 JSON 结果；旧配置不会自动启用。缓存键绑定完整策略，因此新增默认字段会自然隔离旧缓存条目，不迁移旧数据。要求审批/幂等键或有副作用的工具不缓存；命中仍受工具配额、准入、熔断与可靠审计约束。控制台新增白名单编辑和本地只读工具演示；无数据库结构迁移。回退旧版本前需从已保存策略移除 allowed_tools，否则旧解析器会拒绝未知字段；先备份策略，发布去除字段的新版本并清空缓存，再回退。

新增 behavior 与工具深度/并发字段默认不限制；审计、签名远程策略和 Redis 预算通过显式组件注入，不将秘密写进策略 JSON。控制台自动创建独立 audit/ledger 数据库；升级前连同主库备份。新 usage 校验拒绝非整数和不合法子集，按 missing_usage 策略处理。前端升级 Vitest 5 修复已知漏洞，构建要求 Node 22.12+/24/26，使用锁文件。恢复与权限见 OPERATIONS.md。

2026-10-19 新增 `CircuitConfig` 与 `circuit.failure_threshold/recovery_seconds/max_keys`，默认值仍为 5/30/10000，旧本地策略读取时补齐默认值，无数据库结构迁移。修复默认聚合配额下不同模型/工具共用熔断状态；熔断按 Run 内操作种类、目标及身份作用域隔离，准入配额聚合规则不变。新旧 Run 各自保持快照，控制台支持保存与本地故障演示。回退到不认识 circuit 的版本前，备份并从策略源移除整个 circuit 段，再以更高版本发布兼容策略；签名远程策略须由原策略权威重新签发，不能编辑签名缓存或回退高水位。新增默认段也会自然隔离基于完整策略的旧缓存。

本地验证命令：

2026-10-19 在 Windows / CPython 3.11 完成：24 项 Chromium E2E 与 4 项前端单测，TypeScript/生产构建通过。后端全量与无可选依赖安装的最终计数见下方验收记录。新增只读工具缓存身份/版本隔离、审计故障阻断、异步取消/超时清理、策略到期、HTTP 重启复用和浏览器真实命中验证；熔断参数校验、重启保存、模型/工具目标隔离与真实故障阻断也已验证。

独立最小环境仅安装核心 wheel 与 pytest，不安装 FastAPI、Redis、PyYAML、jsonschema 或 fakeredis。真实 Redis 服务未在本机配置，对应测试明确跳过，已建立 Redis 7.4 CI 服务契约；不能把 fake Redis 当作主备持久性验证。

最终验收：完整环境 615 passed、1 skipped（真实 Redis 未配置）、2 subtests passed；最小环境 546 passed、20 skipped、2 subtests passed。隔离 wheel 导入、CLI 与零强制运行依赖检查通过。Python/npm 依赖审计结果见下方记录；前端测试工具为 Vitest 5.0.5。`.[otel]` 为可选依赖，最小环境跳过相关集成测试。

2026-10-19 重新构建 wheel 与独立控制台 ZIP，隔离安装、CLI 和零强制运行依赖检查通过。npm audit 报告 0 vulnerabilities；pip-audit 未发现已知依赖漏洞，本地尚未发布的 agentguard 本身无法匹配 PyPI 漏洞数据，明确跳过。SHA-256 清单见 dist/SHA256SUMS。

```shell
python -m pip install -e ".[test,yaml,quality,redis,redis-test,console,console-test,otel]"
python -m pytest -q
npm --prefix frontend ci
npm --prefix frontend test
npx --prefix frontend playwright install chromium
npm --prefix frontend run test:e2e
python -m pip install --upgrade setuptools wheel
python -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .
python tools/release_check.py
```

wheel 输出目录应仅有一个待验证版本。release_check 在临时 venv 中以 no-index/no-deps 安装 wheel，检查可选依赖缺席下核心导入、CLI 入口、init/check/simulate；若 frontend/dist 已构建，则同时生成独立前端 ZIP（含 API/SDK 兼容版本描述），为所有产物生成 dist/SHA256SUMS。SHA-256 是完整性校验，未提供签名或发布来源证明。前端 E2E 使用独立临时数据库，不写预览策略。AGENTGUARD_TEST_PYTHON 可指定后端 Python。

GitHub Actions 目标为 CPython 3.10–3.14 × Linux/macOS/Windows，另测最小核心与 Redis 服务；前端在 Node 22/Linux 验证锁文件、单测、类型检查、构建和浏览器。工作区已配置，但没有托管 runner 结果。本地 Python pip-audit 与 npm audit 均无已知漏洞，不替代后续持续扫描。本地预算/调用微基准见 BENCHMARKS.md；Redis 故障切换、真实供应商、长期压测与发布签名仍需环境验收。

发布前必须检查未完成项，不能把 local strict profile、普通观测数据库或已写好的 CI 文件标成全系统生产就绪。安装包发布、远程服务部署均尚未执行。
