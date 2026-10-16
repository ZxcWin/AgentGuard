# AgentGuard 本地控制台

Vue 3、TypeScript、Vue Router、Vite；Node.js 22+。前端独立于 Python SDK 安装。

## 启动

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

这是可信局域网中的开发预览，尚无账号登录或用户授权；不等同于生产远程控制平面。不要把供应商密钥放入前端变量。

`--demo` 明确开启本地示例，使用 `.agentguard/demo.db`；无此参数时使用 `.agentguard/runs.db`，不会自动生成数据。示例通过真实 SDK 生命周期执行本地函数，不访问模型供应商。接口故障显示错误，不自动切换数据源。

## 构建与验证

```powershell
npm.cmd run build
npm.cmd test
npx.cmd playwright install chromium
npm.cmd run test:e2e
```

`build` 包含 TypeScript 检查。`test:e2e` 会先构建当前源码，再在 8001 端口启动真实 Python 服务，避免测试陈旧的 dist。12 项 E2E 覆盖概览、筛选分页、详情/调用树、策略校验/版本冲突、工具 JSON/超时配置、幂等键与审批配置、重试表单/尝试事件、断线重试、权限错误、焦点恢复和窄屏导航。Python 测试在项目根目录执行 `.\.venv\Scripts\python.exe -m pytest -q`。

构建后，从项目根目录运行 `agentguard-console --demo`，打开 http://127.0.0.1:8000 。服务直接提供 `frontend/dist/`，Hash 路由支持静态部署，不需要页面回退规则。

## 真实数据与策略范围

页面的接入设置提供 SDK 示例。独立 SDK 进程通过 `SQLiteRunStore` 指向同一数据库路径，运行摘要在 Run 进入/退出时写入，不是逐事件实时上报。进程异常退出时可能留下 running 记录。历史保留清理、后台队列、跨机器采集尚未实现。

策略与版本持久保存在所连接数据库的 `current_policy` 表中，重启后恢复；已有 Run 使用旧快照。独立 SDK 进程需显式配置 `ConfigManager(policy_store=SQLitePolicyStore(同一数据库路径))`，才会在创建新 Run 时读取最新版本，见项目根目录 `examples/console_agent.py`。这不是跨机器策略推送。更新要求 `expected_version`，SQLite 事务原子比较版本，并发冲突返回 409，保存失败不发布新快照。金额以十进制字符串传输，代表 SDK 估算，不是供应商账单。

运行详情通过 `request_id/parent_id` 展示模型与工具父子关系；事件分页未加载完整时会明确标注，不补造调用。默认只存结构性结果摘要，不保存 Prompt、参数或工具输出原文。旧数据库中没有关联字段的历史事件无法追补父子关系。

策略页支持 Run 工具次数、步数、重复调用、单工具额度、副作用等级、幂等键要求、审批要求、审批等待上限、三层超时，以及默认关闭的非流式模型重试、退避、jitter 和重试预算比例。异步超时依赖供应商协作取消，同步调用在执行前后检查；写入/不可逆工具超时后可能已经执行，工具不自动重试。完整语义见 `docs/RUNTIME.md`。

模型筛选按包含该模型的 Run 聚合；多模型 Run 的指标包含其全部调用，不提供逐模型成本拆分。最近运行列表展示该时间范围内的全部模型。

后端只允许本地绑定，并校验 Host 与 Origin；可显式允许可信局域网前端经代理访问。当前版本不包含远程认证、用户授权、组织管理或策略集中下发，部署范围为本地/可信局域网开发预览。

## 接口与部署

OpenAPI：http://127.0.0.1:8000/docs ，原始契约 `/openapi.json`。接口统一使用 `/api/v1`，UTC ISO 时间、`items/total/page/page_size` 分页、`error.code/error.message` 错误结构。查询最长 168 小时，页大小最大 100。

Vite 开发代理把 `/api`、`/docs` 和 `/openapi.json` 转发到 `127.0.0.1:8000`。可在 `.env.local` 中设置 `VITE_API_BASE`（默认 `/api/v1`），见 `.env.example`。本地反向代理部署时，将这些接口路由到 Python 服务，其余静态请求交给 `dist/`，并保留本地 Host。帮助页 API 文档链接随配置的服务地址变化。供应商密钥只能由 Python 服务端管理。
