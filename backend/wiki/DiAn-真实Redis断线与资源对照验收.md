# DiAn 真实 Redis 断线与资源对照验收

日期：2026-11-12。承接[持久服务重启与取消边界验收](DiAn-持久服务重启与取消边界验收.md)。本轮验证真实 Redis 断线后 `/chat` 的恢复、跨进程会话锁、Temporal 服务和 Worker 联合重启，并补一组重复的本机资源对照。所有工单仍为模拟，不涉及真实承运商或赔付。

## 真实 Redis 断线与跨进程锁

运行 `python -m evaluation.check_docker_redis_recovery`。脚本用已有本地 `redis:7-alpine` 镜像创建唯一名称、随机本机端口的临时容器；创建唯一会话和临时案件库，最后停止并自动删除其**自有**容器。实验不触碰其他 Docker 容器。它在受控 `/chat` 路由中使用真实 Redis 的会话锁、回执与状态；Chroma 检索和模型识别由受控替身替代。

| 故障点 | 实测结果 |
| --- | --- |
| 首轮 `/chat` 建案 | 200，案件 `awaiting_confirmation` |
| 关闭本轮专用 Redis 容器，按新客户端请求 ID 发送确认 | 503；修复前为未分类的 500。此注入点在取得会话锁前，不能用来证明已派发副作用时的行为 |
| 同端口启动新的空 Redis，同一确认请求 ID 重试并重放 | 200、`ticket_created`；再次重放仍为同一工单，业务库仅 1 张模拟工单 |
| 两个独立进程争用同用户同会话的 Redis 锁 | 第二进程在第一进程释放前不能进入；释放后进入，两进程正常退出 |

执行输出：`{"offline_http": 503, "recovered_http": 200, "recovered_state": "ticket_created", "ticket_count": 1, "second_blocked_until_release": true, "both_processes_exited": true}`。API 将 Redis 依赖故障明确映射为 503，并提示先核对结果后用相同客户端请求 ID 重试；这不是“故障期间没有产生副作用”的保证。断线实验仅一次，不代表高并发锁公平性、网络分区、Redis 持久恢复或历史聊天可恢复。新增的 Redis 故障 API 回归用例通过；本轮后端全量 **234 项通过、1 条测试依赖弃用警告**。

## Temporal 服务与 Worker 联合重启

运行 `python -m pytest tests/test_temporal_logistics.py::TemporalLogisticsContract::test_persistent_server_and_worker_restart_after_business_commit -q`。真实 Temporal 开发服务使用临时 SQLite 持久文件；独立 Worker 在业务库提交模拟工单后、向 Temporal 回报完成前以 `os._exit(86)` 退出。关闭原服务，再用同一持久文件启动新服务和新 Worker，原客户端请求 ID 取回 `ticket_created`，业务库仍 1 张工单，Run 为 `completed`。

此结果依赖业务库的案件/工单幂等及 Temporal 开发服务持久文件。没有验证生产 Temporal 集群、真正外部 HTTP 工单在联合崩溃后的恢复，也没有验证不同机器的共享存储部署。

## 重复延迟与资源消融

运行 `python -m evaluation.profile_temporal_cost`，原始逐轮延迟与每轮采样结果保存在 [temporal_cost_profile.json](../evaluation/temporal_cost_profile.json)。两路径使用相同 8 案、14 轮的固定数据集，各重复 3 次，顺序交替；每次使用独立临时业务库。受控识别、受控会话、SQLite 模拟工单均相同，差异集中在执行路径。每次均 14/14 符合预期。

| 请求阶段指标，三次中位数 | 默认路径 | Temporal 路径 |
| --- | ---: | ---: |
| 每轮延迟 P50 | 44.73 ms | 539.17 ms |
| 进程树峰值 RSS | 108.50 MiB | 403.23 MiB |
| 采样期间观察到的 CPU 时间 | 0.56 s | 12.98 s |

采样器运行在独立线程，以 50 ms 间隔查看当前 Python 进程及子进程，覆盖默认路径的同步处理与 Temporal 路径的 Worker/开发服务。CPU 为采到的进程计数差值，可能漏掉进程刚启动或退出时的消耗；RSS 是采样峰值。只测**请求阶段**，不含服务启动与关闭。Temporal 首轮包含 Worker 就绪等待，三次 P95 在约 8.22–9.18 秒；默认路径三次 P95 为约 52–84 ms。样本数仅三组、同一 Windows 主机、顺序运行，不能当作统计显著性、生产 SLO 或容量规划。上次[14 轮主链路报告](DiAn-Temporal主链路对照实验报告.md)的数值与这次不同，因运行时环境和新增 Run 记录不同，不应拼接作趋势图。

## 决策与仍未完成的边界

保持 `DIAN_TEMPORAL_LOGISTICS=false`。目前可证明受控故障中的结果恢复与单工单边界，但三轮对照显示明显的本机额外延迟和进程成本。外部真实系统还需要权威结果查询、稳定幂等键和主体归属校验。下一步仅在有明确可靠性收益目标时，再验证真实 HTTP Provider 与 Redis 同时参与的故障矩阵、长时间负载和生产式 Temporal 存储；在这些证据出现前不默认开启 Temporal，也不宣称生产级 Agent Infra。
