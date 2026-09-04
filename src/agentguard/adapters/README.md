# Adapters

放置 Port 的具体实现以及第三方集成：内存/SQLite/Redis、文件/Prometheus/OpenTelemetry、模型供应商和 Agent 框架适配器。

适配器必须可选安装、薄、可替换；不能在这里复制预算、限流或重试规则。
