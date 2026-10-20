# AgentGuard 本地控制台

Vue 3、TypeScript、Vue Router、Vite；Node.js 22.12+ 或受支持的 24/26 LTS。前端独立于 Python SDK 安装。六个页面包含概览、运行记录、策略、审计、成本账本与接入设置。

精确缓存支持开关、TTL、容量、模型及只读工具白名单编辑，显示实际 memory/SQLite 类型并按作用范围清空。持久后端由服务启动参数显式选择，清空纳入可靠审计，见 docs/CACHE.md。演示模式概览页可以生成模型或只读工具缓存调用，在运行详情查看真实命中；工具示例要求显式允许 `catalog_lookup` 且副作用等级为 `read`，命中仍计工具调用与配额。

防护策略页支持生产严格模式，校验预算、价格与安全阻断设置后保存；故障熔断阈值、恢复等待和作用域容量也可编辑。最新验证：4 项单测、24 项浏览器 E2E，以及 TypeScript/生产构建通过。审计、登录、账单导入与对账见 [`docs/OPERATIONS.md`](../docs/OPERATIONS.md)，执行边界见 [`docs/RUNTIME.md`](../docs/RUNTIME.md)。

## 启动

依赖装好后也可在根目录执行 `.\.venv\Scripts\python.exe tools/dev.py --demo --host 192.168.3.185`，一起启动前后端，Ctrl+C 同时停止。IP 使用本机实际地址；本机预览省略 host。

在项目根目录安装可选服务依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[console,console-test,yaml]"
```

终端一，在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m agentguard.api.http --demo
```

终端二，在 `frontend/` 目录运行：

```powershell
npm.cmd ci
npm.cmd run dev
```

打开 http://127.0.0.1:5173 。Windows 若禁止运行 npm.ps1，可使用 npm.cmd。

### 通过本机局域网 IP 预览

默认前后端只监听回环地址。需要在局域网访问时，前端监听本机指定 IP，后端仍监听 127.0.0.1，通过前端代理访问。以 `192.168.3.185` 为例：

```powershell
# 项目根目录，后端精确允许这个前端来源；不放开任意 Origin
.\.venv\Scripts\python.exe -m agentguard.api.http --demo --allow-origin http://192.168.3.185:5173
# frontend 目录
npm.cmd run dev -- --host 192.168.3.185
```

打开 http://192.168.3.185:5173 ，API 文档也可通过此地址的 `/docs` 访问。Vite 对 `/api`、`/docs`、`/openapi.json` 代理到本机后端并改写 Host；浏览器 Origin 仍由后端精确验证。`--allow-origin` 可重复指定，IP 改变后需要更新启动参数。`npm.cmd run dev:lan` 可监听所有网卡；优先使用指定 IP 的方式。

这是可信局域网开发预览，默认匿名管理员。需要身份控制时添加 credentials 并分发角色凭证，页面出现连接对话框。公网部署须使用 HTTPS 反向代理与服务端授权，完整参数见运维文档。不要把供应商密钥放入前端变量。

`--demo` 明确开启本地示例，使用 `.agentguard/demo.db`；无此参数时使用 `.agentguard/runs.db`，不会自动生成数据。示例通过真实 SDK 生命周期执行本地函数，不访问模型供应商。接口故障显示错误，不自动切换数据源。

## 构建与验证

```powershell
npm.cmd run build
npm.cmd test
npx.cmd playwright install chromium
npm.cmd run test:e2e
```

`build` 包含 TypeScript 检查。`test:e2e` 先构建当前源码，再在 8001 端口启动真实 Python 服务。24 项 E2E 覆盖运行/策略完整链路、审计、真实账本导入/去重/调整、登录交互、只读角色、审批/重试/熔断/Schema/安全/脱敏/模型与工具缓存/遥测、断线与窄屏。登录交互用受控响应验证，实际服务端凭证和权限另由 Python 集成测试验证。Python 测试在根目录执行 `.\.venv\Scripts\python.exe -m pytest -q`。E2E Python 路径自动适配 Windows/POSIX，也可通过 AGENTGUARD_TEST_PYTHON 指定。

构建后，从项目根目录运行 `agentguard-console --demo`，打开 http://127.0.0.1:8000 。服务直接提供 `frontend/dist/`，Hash 路由支持静态部署，不需要页面回退规则。

## 真实数据与策略范围

页面的接入设置提供 SDK 示例。独立 SDK 进程通过 `SQLiteRunStore` 指向同一数据库路径，运行摘要在 Run 进入/退出时写入；另可注入 EventSink 后台逐事件导出。进程异常退出时可能留下 running 记录。HTTP 服务默认每分钟清理超过 7 天的已结束运行，保留期可配置为 1–30 天。设置页显示当前服务进程的队列、失败、丢弃与清理计数，不包含其他 SDK 进程的导出计数；跨机器采集尚未实现。

策略与版本持久保存在所连接数据库的 `current_policy` 表中，重启后恢复；已有 Run 使用旧快照。独立 SDK 进程需显式配置 `ConfigManager(policy_store=SQLitePolicyStore(同一数据库路径))`，才会在创建新 Run 时读取最新版本，见项目根目录 `examples/console_agent.py`。这不是跨机器策略推送。更新要求 `expected_version`，SQLite 事务原子比较版本，并发冲突返回 409，保存失败不发布新快照。金额以十进制字符串传输，代表 SDK 估算，不是供应商账单。

运行详情通过 `request_id/parent_id` 展示模型与工具父子关系；事件分页未加载完整时会明确标注，不补造调用。默认只存结构性结果摘要，不保存 Prompt、参数或工具输出原文。旧数据库中没有关联字段的历史事件无法追补父子关系。

策略页支持 Run 工具次数、步数、重复调用、单工具额度、副作用等级、幂等键要求、审批要求、审批等待上限、三层超时，以及默认关闭的非流式模型重试、退避、jitter 和重试预算比例。异步超时依赖供应商协作取消，同步调用在执行前后检查；写入/不可逆工具超时后可能已经执行，工具不自动重试。完整语义见 `docs/RUNTIME.md`。

模型筛选按包含该模型的 Run 聚合；多模型 Run 的指标包含其全部调用，不提供逐模型成本拆分。最近运行列表展示该时间范围内的全部模型。

策略页支持内容安全与非流式结构化脱敏，严格 JSON/Schema/文本规则，以及预算内重试/请求修复/备用模型/应用人工决定。修复与人工需要实际 SDK 应用注册回调；人工需要可靠审计，前端不执行回调代码。演示模式概览提供安全检测与质量检查示例，遵循保存的策略；未启用检查时不会产生相应拦截。详情时间线不展示原文。流式/已执行子工具的调用不重放，脱敏模式拒绝流式请求。见 `docs/QUALITY.md`、`docs/CONTENT_SECURITY.md`。

后端默认本地绑定；远程绑定须配置 credentials 与 public-host，逐请求校验角色，支持精确 CORS。签名策略分发可选开启。组织管理、SSO 和多租户行级授权不在本版范围；同一数据库是一个权限工作空间。

## 接口与部署

OpenAPI：http://127.0.0.1:8000/docs ，原始契约 `/openapi.json`。接口统一使用 `/api/v1`，UTC ISO 时间、`items/total/page/page_size` 分页、`error.code/error.message` 错误结构。查询最长 168 小时，页大小最大 100。

Vite 开发代理把 `/api`、`/docs` 和 `/openapi.json` 转发到 `127.0.0.1:8000`。可在 `.env.local` 中设置 `VITE_API_BASE`（默认 `/api/v1`），见 `.env.example`。本地反向代理部署时，将这些接口路由到 Python 服务，其余静态请求交给 `dist/`，并保留本地 Host。帮助页 API 文档链接随配置的服务地址变化。供应商密钥只能由 Python 服务端管理。
