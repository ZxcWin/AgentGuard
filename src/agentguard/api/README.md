# API

放置稳定的 Python 对外入口：`AgentGuard`、`RunContext`、装饰器、Wrapper 和公共 DTO 转换。

API 层负责参数校验和依赖组装，不复制策略逻辑。内部实现通过 `application` 完成。
