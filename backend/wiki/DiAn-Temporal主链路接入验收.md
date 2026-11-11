# DiAn Temporal 物流主链路接入与验收

日期：2026-11-11。范围：`/chat` 路由识别为物流案件后的执行。此功能默认关闭，以 `DIAN_TEMPORAL_LOGISTICS=true` 开启。普通客服请求、模型意图识别、Redis 会话、案件展示与现有物流领域规则继续使用原路径。

## 设计与责任边界

客户端应在重试时复用同一个 `client_request_id`；未提供时 API 只为本次请求生成新 ID。物流 Agent 将用户、会话、消息和当轮案件状态写入与物流案件相同的 SQLite 文件中的 `temporal_dispatch` 表。单独的 Worker 扫描 `pending`，使用由用户、会话、请求 ID 派生的稳定 Workflow ID 启动 `LogisticsTurn`。API 等待该执行完成，并把领域返回的案件状态写回原有会话回执。开始执行与标记已投递之间若崩溃，下一次扫描仍使用同一 ID，并拒绝重复启动。

Activity 调用原有 `LogisticsService.process()`。其 SQLite 事务仍保证**模拟**案件与**模拟**工单同事务落库；Temporal 管理 Worker 重试、历史与跨进程执行恢复。`temporal_dispatch` 不保存长期 HTTP 响应，原会话回执仍由 Redis 管理。案件关联现已在业务库的 `active_cases` 表中持久化：Redis 状态缺失时，案件查询与后续确认可从业务库恢复；这不等于 Redis 历史消息或回执本身已持久化。

本次没有把实验中的 HTTP Provider 和 PostgreSQL Operation 账本接入生产路径。对真实工单系统不可声称恰好一次；接真实 Provider 前必须拆分短事务准备、外部调用和结果核对，并把确认绑定案件版本、动作和参数。

## 启动

宿主机：在 `backend` 目录配置 `.env` 的 `DIAN_TEMPORAL_LOGISTICS=true`、`DIAN_TEMPORAL_ADDRESS`、`DIAN_LOGISTICS_DB`，启动 Temporal 开发服务，再分别运行 `python -m core.temporal_logistics` 和原 FastAPI 服务。两进程必须指向**同一**物流数据库文件。部署时不可把宿主机和容器中的 `localhost` 当成同一地址。

Docker 验证环境可使用 `docker compose -f docker-compose.yml -f docker-compose.temporal-check.yml up -d --build`。Overlay 只用于开发验收；Temporal `start-dev` 不具备生产服务配置。当前主机的 Docker Desktop 因 Secrets Engine 本地 socket 错误无法启动，本轮验收使用 SDK 启动的真实 Temporal 开发服务，未验证上述容器组合。

## 已完成验收

运行：`cd backend` 后执行 `python -m pytest tests/test_temporal_logistics.py -q`。环境：Windows Python 3.11.9、Temporal Python SDK 1.36.0，测试自动启动 Temporal CLI 1.10.0 / Server 1.32.2。结果：3 个集成测试通过；原有完整测试集在加入第三个测试之前为 207 项通过，物流相关回归为 32 项通过。

| 断言 | 结果 |
| --- | --- |
| 同一 `/chat` 两轮：核验后等待确认、明确确认后建单 | 通过 |
| 开关关闭与开启后的案件状态、工单编号一致 | 通过 |
| 相同请求再次执行返回同一工单 | 通过 |
| 相同请求 ID 携带不同消息被拒绝 | 通过 |
| 请求先落入待投递表，Worker 后启动仍完成执行 | 通过 |
| 案件和工单仍在原 SQLite 领域库持久化 | 通过 |

此前[隔离故障实验](../../experiments/temporal-poc/README.md)的 6 组场景证明 Temporal 恢复与业务结果核对的边界。后续[主链路对照实验](DiAn-Temporal主链路对照实验报告.md)已覆盖 `/chat` 独立 Worker 崩溃、API 等待取消和受控会话指针丢失；真实 Redis TTL、Temporal 服务重启、真实外部 Provider 与资源成本仍未验收。

## 下一道门槛

修复案件关联后已重跑[主链路对照实验](DiAn-Temporal主链路对照实验报告.md)：8 案件、14 轮两组业务结果一致；受控会话指针丢失后，两组均可读取数据库中的当前案件。下一阶段应优先验证独立 HTTP 工单 Provider 的稳定业务键、未知结果核对和确认版本绑定。真实 Redis、同案并发、Temporal 服务重启和资源成本仍是默认启用前的门槛。
