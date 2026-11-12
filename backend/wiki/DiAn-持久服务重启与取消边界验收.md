# DiAn 持久服务重启与取消边界验收

日期：2026-11-12。承接[Temporal 接收与并发故障验收](DiAn-Temporal接收与并发故障验收.md)，本轮检查服务自身重启、同案两次确认和请求等待取消。仍使用模拟订单与独立 HTTP 模拟工单，不接入真实承运商、退款或赔付。

## 实验条件与结果

| 场景 | 受控结果 | 不能外推的部分 |
| --- | --- | --- |
| Temporal 开发服务使用同一 SQLite 持久文件：Workflow 已派发、Worker 未启动时关闭服务，再启动新服务和 Worker | 原 Workflow 继续执行，案件进入 `awaiting_confirmation`，Run 为 `waiting_confirmation` | 未测生产 Temporal 集群、存储故障或高可用切换 |
| 消融：两个独立的内存持久化开发服务 | 第一服务中的待执行 Workflow 在第二服务中不可查询 | 仅证明开发服务持久配置的必要性 |
| 同案两个不同请求在独立进程中同时确认（原执行路径） | 两个请求均取回 `ticket_created`，同一远端编号，远端 1 张工单 | 未测高负载、跨地域或不支持幂等键的真实 Provider |
| 同案两个 Temporal Workflow 同时确认（同一领域服务） | 均为 `ticket_created`，远端 1 张工单 | Worker 在本机受控测试环境中运行 |
| 独立 HTTP 服务已提交工单后，调用方取消等待 | 调用任务被取消；领域线程完成后案件为 `ticket_created`，远端 1 张工单 | “取消等待”不能声明远端写操作已撤销 |

运行 `python -m pytest tests/test_temporal_logistics.py tests/test_logistics_http_operation.py -q` 可复现上述断言。持久重启测试使用 Temporal Python SDK 的 `dev_server_database_filename` 和临时 SQLite 文件；内存对照使用默认 `start_local()`。这属于开发服务进程重启验收，不是生产部署方案。

## 受控主链路再对照

运行 `python -m evaluation.compare_temporal_logistics`，原始逐轮结果写入 [temporal_comparison.json](../evaluation/temporal_comparison.json)。本轮 8 个案件、14 个轮次，两路径均 14/14 符合预期，逐轮业务状态和工单数一致，意外工单 0。原路径 P50 34.19 ms、P95 98.54 ms；Temporal 路径 P50 476.05 ms、P95 5630.79 ms；Worker 业务提交后退出，替换 Worker 的受控恢复约 16.47 秒，仍为 1 张模拟工单。

这些数字包含开发服务冷启动、0.5 秒待投递扫描和本机 ASGI 路径，样本仅 14 轮、顺序执行。该对照使用 SQLite 模拟工单和受控会话替身，**不是**真实 HTTP Provider 与 Redis 的吞吐、资源或生产延迟对照。没有测 CPU、内存、部署运维成本，不能据此改默认执行引擎。

## Redis TTL：独立 Docker 实例验收

2026-11-12 重新启动本机 Docker Desktop 后，引擎可连接。在已有本地 `redis:7-alpine` 镜像上启动临时容器 `dian-ttl-check-20261112`，仅把容器 6379 映射到主机 `127.0.0.1:16379`，没有挂载数据卷，也没有启动或修改仓库中的其他容器。验收命令（运行 Python 前进入 `backend` 目录）：

```powershell
docker run -d --rm --name dian-ttl-check-20261112 -p 127.0.0.1:16379:6379 redis:7-alpine
$env:DIAN_REDIS_TEST_URL="redis://127.0.0.1:16379/0"
python -m evaluation.check_real_redis_ttl
docker stop dian-ttl-check-20261112
```

脚本使用唯一会话名写真实 Redis 回执和会话状态，设置 2 秒 TTL，等待过期后通过 `/logistics/case` 验证业务库中的案件仍可回查。实测输出：`{"real_redis": true, "receipt_expired": true, "state_expired": true, "case_visible_after_ttl": true}`；停止容器前 `redis-cli dbsize` 为 0。容器已停止并由 `--rm` 删除。这只覆盖真实 Redis 的短 TTL 与案件回查，不代表 Redis 断线、多实例锁竞争、`/chat` 完整链路或聊天历史恢复。

## 决策边界与下一步

默认 `DIAN_TEMPORAL_LOGISTICS=false` 维持不变。Temporal 负责工作流执行恢复；同案外部写操作最多一张还依赖业务库 Operation、稳定键及模拟服务唯一约束。HTTP 请求等待取消不撤销已派发副作用，管理员挂起也只阻止后续自动推进。真实系统必须先确认权威结果查询、幂等键和归属校验契约。

下一步在隔离环境验证 Redis 断线与多实例锁、持久化 Temporal 服务与 Worker 同时重启，以及带 CPU/内存采样的重复对照；再做是否默认开启的成本收益决策。本轮仍未满足这些门槛，不能称为生产级可靠执行平台。
