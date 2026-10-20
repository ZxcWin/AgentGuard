# 离线命令行

另有 `agentguard audit-deliver --source app.audit.db --archive archive.db --limit 100`，将有界审计批次投递到独立持久归档，提交后确认并按 event_id 去重；不执行原始业务操作。使用、逻辑保留和故障恢复见 [运维文档](OPERATIONS.md)。

安装核心包即可使用 `agentguard`，也可用 `python -m agentguard`。命令不读取 `AGENTGUARD_*` 环境覆盖，不导入供应商或执行工具。配置支持 JSON；YAML 需要额外安装 `agentguard[yaml]`。配置 JSON 与 fixture 限制为 1 MiB。

```shell
agentguard init --output agentguard.json
agentguard check --config agentguard.json
agentguard explain --config agentguard.json
agentguard simulate --config agentguard.json --fixture request.json
agentguard replay requests.json --config agentguard.json
```

`init` 只创建新文件，不覆盖现有配置。生成开发预设，包含本地合成模型 `local-example` 的零价和初始预算；接入真实模型必须修改模型名与价格。生成配置不代表生产预设。

`check` 验证字段、严格预设和所需可选依赖，不连接 Redis 或模型服务。`explain` 与 `simulate` 复用 SDK 执行一条本地 fixture，输出决策、汇总及安全事件代码。explain 说明本次实际路径，不穷举未触发规则。

单条 fixture：

```json
{
  "model": "local-example",
  "messages": "local offline example",
  "max_tokens": 32,
  "response": {
    "content": "{}",
    "usage": {"input_tokens": 4, "output_tokens": 8}
  }
}
```

`replay` 读取 1–100 条上述对象组成的 JSON 数组，始终为 shadow 模式。所有记录共享一个 Run 的预算，每条使用文件内的静态响应，不执行真实模型、工具或动态代码。质量重试会重新读取同一个静态响应；不是供应商回归测试。拒绝结果也保留已消耗用量。仅支持 memory 准入后端；Redis 配置可 check，但 simulate/replay 会在连接前拒绝。

正常结束返回 0；策略拒绝返回 1；配置、输入、依赖或文件错误返回 2。输出为单个 JSON 对象，错误不回显原配置或异常内容。`--help/--version` 使用标准文本格式。回放结果不输出 Prompt 或响应原文；请勿把秘密放入模型名等操作元数据。
