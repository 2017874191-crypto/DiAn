# DiAn P1 业务接口阶段验收记录（2026-11-09）

## 本阶段交付

在 `core/logistics_providers.py` 定义四个业务接口：

| 接口 | 当前实现 | 边界 |
| --- | --- | --- |
| `OrderProvider` | `MockOrderProvider` | 按用户核对订单归属与订单/运单一致性。 |
| `TrackingProvider` | `MockTrackingProvider` | 返回模拟轨迹或明确的查询失败。 |
| `TicketProvider` | `SQLiteTicketProvider` | 以案件 ID 为幂等键，在同一 SQLite 事务内创建模拟工单。 |
| `EvidenceProvider` | `DeclarationEvidenceProvider` | 仅识别用户“已提交材料”的声明，不处理真实文件。 |

`LogisticsService` 通过构造参数接收提供者，默认使用上述模拟实现；Agent、HTTP 入口和前端接口契约无需随模拟实现搬迁。若替换订单提供者，必须同时指定物流提供者，避免把真实订单与模拟轨迹拼接成伪造事实。工具轨迹的 `source` 由提供者结果给出。

## 验收证据

- Linux x86_64 / Python 3.12 容器安装 `requirements-dev-lock-linux-py312.txt`，依赖检查通过。
- 数据集结构校验通过：50 案、73 轮；未重新核验上游源文件缓存。
- 物流领域、API 契约、评测器和业务接口测试共 28 项通过。
- 隔离 Docker 环境的 `DIAN-001` HTTP 冒烟回放通过：进入 `awaiting_evidence`，工单数 0，调用订单、物流和政策工具。报告位于 `data/acceptance/eval/p1-provider-smoke.json`。
- 上一阶段的 30/30 dev 集报告生成于本次接口改造前；本次没有重新运行全量模型回放，不能把旧报告冒充为新代码的全量评测。

## 未完成的 P1 工作

1. 尚无真实订单、承运商、工单和文件服务的 adapter，也没有生产凭据与契约环境。
2. 当前 `TicketProvider` 使用 SQLite 连接参数以保持案件/模拟工单的本地原子提交。真实外部写操作不能直接放进该事务；下一阶段须设计持久化操作意图、外部幂等键、执行结果查询和“结果未知”状态，再接真实工单系统。
3. 真实 provider 需要统一设置超时、限流、错误码映射、日志脱敏和重试策略；查询失败不能写成成功事实。
4. `EvidenceProvider` 尚未验证图片/文件。需要对象存储引用、上传者、哈希、类型、扫描结果及人工审核状态。
5. SQLite 仍是单机演示存储；多副本生产部署需要正式数据库、唯一约束、索引和迁移方案。

因此本记录只验收“接口边界与模拟实现保持原行为”，不代表已接入真实业务系统或 P1 整体完成。
