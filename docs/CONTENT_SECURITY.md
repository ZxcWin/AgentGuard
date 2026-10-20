# 内容检测与结构化脱敏

安全检测默认关闭；启用后默认 `block`。严格生产配置要求密钥阻断。开发模式可显式设置 `security.action=redact`，在请求发送前、普通响应返回前以及响应拦截器之后对命中值脱敏。

```python
from agentguard import AgentGuard

guard = AgentGuard({"security": {
    "enabled": True, "action": "redact", "detect_pii": True,
}})
with guard.run() as run:
    result = run.invoke(
        lambda **kw: {"content": "联系 user@example.com"},
        messages=[{"role": "user", "content": "邮箱 user@example.com"}],
    )
```

常见密钥、字符串凭据字段、邮箱和中国大陆手机号被替换为 `[REDACTED:规则代码:不透明标识]`。标识使用独立随机密钥的 HMAC，避免对低熵手机号直接散列；同一个 SecurityGuard 实例中相同规则和命中值产生相同标识。SDK 不保存可逆替换表，不导出标识或原值到事件。默认按 Run 创建的检测器不承诺跨 Run 稳定；需要在受控范围关联时显式注入同一 SecurityGuard。

转换复制普通字符串、dict、list、tuple，保留键、容器类型、数值、布尔及 null，不修改输入。合法序列化 JSON 对象/数组先解析再处理，避免破坏引号和转义。凭据字段识别 api_key、secret、token、password、authorization、access_token、refresh_token 等字符串值；检测属于启发式规则，不证明所有敏感数据已识别。

命中 Prompt 注入、敏感字典键、无法转换的 SDK 对象/二进制结构、循环或超限内容时阻断。纯净 SDK 响应可保持原对象；包含敏感内容的 SDK 对象须由供应商适配器转换为普通 JSON。流式请求在调用前拒绝，因为分片脱敏无法撤回已发送内容。检测器或脱敏器异常同样拒绝，不把异常原文写入事件。

脱敏后重新扫描并执行质量检查，JSON Schema 不符合或仍存在发现时拒绝。输入 token 在脱敏之后重新估算；供应商响应 usage 在脱敏前结算，不能通过改写输出降低已发生的费用。发生安全发现/脱敏的请求和响应均不缓存，即使调用方声明 public。

普通事件只包含规则代码、阶段、动作和替换数量。配置可靠审计后，安全决定在放行前落盘；审计故障不放行。自动脱敏不代替数据分类、应用授权、完整语义防护或供应商数据处理约定。
