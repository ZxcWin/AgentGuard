# API 层

放置稳定的 Python 对外入口：`AgentGuard`、`RunContext`、装饰器、Wrapper 和公共 DTO 转换。

API 层负责参数校验和依赖组装，不复制策略逻辑。内部实现通过 `application` 完成。

`http.py` 提供可选的本地控制台 HTTP 适配层，通过 `agentguard[console]` 安装。版本化接口为 `/api/v1`，OpenAPI 位于 `/openapi.json`；不从核心公共入口自动导入，因此 SDK 安装不要求 FastAPI。范围与运行命令见 `frontend/README.md`。

`console_models.py` 固定响应 DTO；策略通过 `SQLitePolicyStore` 保存版本和配置，原子比较 `expected_version`。
接口仅面向本机开发，Host/Origin 检查不等同于远程用户认证。工具和超时配置复用 SDK schema，
HTTP 层不另实现规则。运行树通过已经持久化的 request/parent 关联构建，不提供逐事件实时推送。

可通过 `--allow-origin` 精确允许可信局域网前端经本机代理访问，后端仍只监听回环地址。
重试策略同样复用 SDK schema；演示接口 `POST /api/v1/demo/runs?scenario=retry` 模拟首次连接失败，
遵循当前策略，不自动打开重试。真实数据模式下所有 demo 写入返回 404。
