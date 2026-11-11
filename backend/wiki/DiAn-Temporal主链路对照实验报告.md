# DiAn 物流主链路 Temporal 对照实验报告

实验日期：2026-11-11（Asia/Shanghai）。代码基线：`f2f7567`。结论：在相同的受控物流输入下，两条路径的业务结果一致；Temporal 路径提高了本实验中已提交操作的结果可取回性与 Worker 崩溃后的执行恢复，但正常请求延迟明显增加。此轮证据**不足以**把 Temporal 改成默认路径。

## 实验范围与可复现性

脚本：[对照实验程序](../evaluation/compare_temporal_logistics.py)；原始逐轮记录：[JSON 结果](../evaluation/temporal_comparison.json)。在 `backend` 目录执行：

```shell
python -m evaluation.compare_temporal_logistics
```

选取 `evaluation/datasets/dian_logistics_v1/test.jsonl` 中 8 个固定案件（`DIAN-007/008/013/014/033/034/049/050`），共 14 个用户轮次，覆盖破损待举证、丢件确认建单、签收争议转人工等。数据文件 SHA-256：`74644e23b36bd8aeb45bd3b00ab89c2b241fd116c564a7f0472a5a079930fcaa`。每组使用独立临时 SQLite 案件库，相同案件 ID、相同消息、相同用户和相同确认顺序。

请求经过 FastAPI `/chat` 路由和物流 Agent 执行路径。为隔离执行框架的影响，意图类别直接使用所选数据集的标注值，订单号由同一正则提取；会话内存是受控替身，未调用付费模型或真实 Redis。Temporal 组使用 SDK 启动的真实本地服务与**独立进程** Worker；原路径直接执行原有 `LogisticsService`。两组均使用同一版领域规则与 SQLite 模拟工单。测试环境为 Windows、Python 3.11.9、Temporal Python SDK 1.36.0；Temporal 开发服务使用内存持久化，不能代表服务自身重启恢复能力。

## 无故障对照

| 指标 | 原路径 | Temporal 路径 |
| --- | ---: | ---: |
| 案件 / 用户轮次 | 8 / 14 | 8 / 14 |
| 状态与预期工单数均正确 | 14 / 14 | 14 / 14 |
| 两组逐轮状态、问题类型和工单数 | 相同 | 相同 |
| 非预期工单 | 0 | 0 |
| 本轮请求延迟 P50 | 18.76 ms | 488.76 ms |
| 本轮请求延迟 P95 | 38.17 ms | 5639.52 ms |
| 去掉各组首轮后的 P50 | 18.29 ms | 479.44 ms |

延迟为本机 ASGI 请求的墙钟时间，包含工作流启动、待投递扫描及返回结果；不包含真实网络、Redis、模型或外部工单服务。两组顺序执行，只有单次样本；14 次请求的最近秩 P95 等于最大值，Temporal 最大值受开发服务和 Worker 冷启动影响。稳定轮次仍多出约 0.46 秒，其中待投递扫描周期配置为 0.5 秒。**不能把这些数字直接外推成生产 SLO 或统计显著差异。**

## 故障对照

| 注入位置 | 原路径 | Temporal 路径 | 结论 |
| --- | --- | --- | --- |
| 模拟工单事务提交成功，最终 API 回执写入失败；同 ID 重试 | 返回 503 `tool_outcome_unknown`，DB 中 1 张工单 | 返回 200 `ticket_created`，DB 中 1 张工单 | 原路径选择保守阻止重放；Temporal 可重取已完成结果 |
| 已落库后、向执行引擎报告成功前强制终止 Worker（`os._exit(86)`），启动第二个 Worker | 不适用：原路径无独立 Worker | 原请求返回 200，状态 `ticket_created`，DB 中 1 张工单；恢复约 16.55 秒 | Temporal 的 Activity 超时后由新 Worker 接管；模拟工单原有 SQLite 幂等也在起作用 |
| API 请求落入待投递表、Worker 尚未启动时取消等待；启动 Worker 后同 ID 重试 | 未在本组测试 | 返回 200 `awaiting_confirmation`，0 张工单 | 待投递记录能够跨 API 等待取消继续执行 |
| 清除会话中的案件指针，再调用 `/logistics/case` | 返回空；SQLite 仍有 1 张工单 | 同样返回空；SQLite 仍有 1 张工单 | Temporal 未解决案件关联回查缺口 |

模拟工单的 `case_id` 唯一约束和同事务保存来自原有领域实现，因此两条路径在这些场景下都没有重复工单。隔离实验中真实 HTTP Provider 的“响应丢失”结果不能直接归入本次主链路实验；真实 Provider 尚未接入。

## 决策与后续门槛

维持 `DIAN_TEMPORAL_LOGISTICS=false` 默认值。若目标是可恢复的外部副作用，下一步先把案件关联写入持久层，并将模拟同事务建单改成具备稳定业务键、结果查询与未知状态的真实 Provider 适配，再做相同故障矩阵。还需验证两次不同请求对同一案件的并发确认、Temporal 服务重启、Redis 过期后的请求恢复、取消时远端操作仍可能完成，以及故障下的重试预算和资源占用。完成这些且收益超过延迟与运维成本后，再决定是否默认启用。

本实验只是一次有限样本的工程消融。受控识别可能掩盖真实路由错误；模拟会话未覆盖分布式锁与 Redis TTL；开发版 Temporal 未覆盖服务高可用；未测 CPU、内存和生产部署成本。所有未测项目均不得宣称已验收。
