# DiAn 物流查询失败回复验收

日期：2026-11-19。范围：优化方案中只读物流查询的超时提示及本地订单归属拒绝。产品修改提交为 `70dc13a`。本轮没有接入写操作，也没有扩展 Temporal 或故障组合矩阵。

## 已复现的问题与修改

使用真实意图、回答模型和实际 ASGI `/chat`，以测试 transport 立即抛出 `httpx.ReadTimeout`，不连接快递鸟。首次回答虽然没有编造轨迹，却自行补充“查询时间：刚刚”，并询问“需要我过一会儿再帮你重试一次吗”。项目没有后台定时查询能力。同轮另一身份的订单归属校验在本地失败，回复却以“我帮你查了……物流轨迹”开头，混淆了本地校验与外部查询。

修改限定在已有适配器、工具说明及验证脚本：

- 超时返回 `error_code=tracking_timeout`，生产适配器记录 `query_performed=true` 和请求尝试开始时间 `queried_at`。该布尔值只代表尝试发起请求，不代表供应商成功响应。没有取得轨迹，不能推断包裹状态。
- 工具说明明确超时后由用户主动发起新查询，不承诺后台重试或定时查询。
- 本地归属拒绝仍为 `query_performed=false`；只建议核对订单号、登录账号或由服务端核实绑定，不索取不能解除归属限制的电话尾号。
- 在现有验证脚本增加 `--simulate-timeout`，复用同一条 API、模型和工具路径。模拟结果单独标记 `source=simulated_kdniao_timeout`、`query_mode=simulated_timeout`、`query_performed=false`、`queried_at=null`，另存 `simulated_at`，不会冒充真实查询。

没有增加测试函数；仅在已有超时协议测试中补充错误码和查询元数据断言。

## 最终结果

模型为报告中记录的 `deepseek-v4-pro`。Redis 使用 fakeredis，案件与工单使用临时 SQLite，身份来自 Bearer 映射，订单归属来自合成的服务端绑定。

| 场景 | transport 尝试 | 快递鸟外部请求 | 结果 |
| --- | ---: | ---: | --- |
| 授权订单模拟超时 | 1 | 0 | 返回超时结构化结果；回复说明未取得轨迹，不能判断包裹状态，需要用户主动重新查询，无后台自动或定时重试 |
| 同一 client_request_id 重复请求 | 0 | 0 | 完整回复和结构化结果与第一次相同 |
| 另一 Bearer 身份在请求体冒用原用户 | 0 | 0 | 按可信身份在本地拒绝；不推断包裹状态，不索取电话尾号解除限制 |
| 已有 16 节点签收样本回放 | 1 | 0 | 保留历史响应来源；最新事件仍为成都、2026-11-16 18:09:02、“快件领取成功”，没有把记录当成本人收到的证明 |

最终超时链路与签收回放各完成三次 HTTP 请求，状态均为 200，工单数均为 0；每组实际模型调用 5 次，回执重放不再次调用模型或供应商。HTTP 200 表示请求被处理，物流查询成功与否以 `tracking_query.success` 为准。

最终超时回复明确说：“当前我没有后台自动重试或定时查询的能力，所以需要你主动再来问一次。”正文没有稳定复述“模拟”来源，该点保留为展示限制；结构化字段与实验报告明确标注模拟，不能将本轮用于证明真实供应商超时表现。

相关测试命令：

```powershell
python -m pytest -q tests/test_kdniao_tracking.py tests/test_agent_orchestrator.py tests/test_chat_reliability.py tests/test_intent_recognizer.py
```

结果为 52 项通过，1 条既有依赖警告。[原始回复、工具轨迹、自动检查及模型辅助语义复核](../evaluation/kdniao_failure_acceptance.json)保留首次失败、最终候选和签收回归。首次报告的提交号为修改前 HEAD，实际脚本状态以报告内源码哈希为准；最终两组均在 `70dc13a` 上执行。中间调整另跑过一组，包含在本轮实际消耗中：四组累计模型调用 20 次、快递鸟调用 0 次。

## 复现与停止边界

在 backend 目录，使用本机模型配置运行：

```powershell
python -m evaluation.verify_kdniao_chat --simulate-timeout --output data/kdniao/timeout-chat.json
python -m evaluation.verify_kdniao_chat --response-fixture evaluation/fixtures/kdniao_signed_redacted.json --output data/kdniao/signed-regression.json
```

两条命令均使用真实模型，消耗模型调用；均不请求快递鸟。脚本会检查 HTTP 结果、一次查询尝试、回执相同、归属拒绝及工单数量，回复语义复核来源为 Codex 辅助检查，不冒充独立人工复核。

测试 transport 立即触发超时异常，没有等待真实供应商 10 秒，也没有证明网络取消、真实超时耗时或生产 Redis 行为。每个场景只有一个已曝光开发请求，不报告总体成功率、P95 或统计显著收益。回复中的 `escalated` 仍是既有启发式标记，不代表人工实际接单。

本项到此停止扩展。后续有正确寄件人或收件人验证尾号后，补一票有轨迹的真实只读 `/chat` 查询；不阻塞已有 20 案、28 轮物流期望复核与固定版本评测。真实订单授权、外部写操作和独立人工复核分别按原方案边界推进。
