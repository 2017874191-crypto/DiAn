# DiAn 真实 Redis 与独立 HTTP 工单联合故障对照验收

日期：2026-11-12。承接[真实 Redis 断线与资源对照验收](DiAn-真实Redis断线与资源对照验收.md)。本轮只回答一个问题：当远端模拟工单已提交、HTTP 响应丢失，且最终 API 回执因 Redis 断线无法保存时，默认路径与可选 Temporal 路径是否有不同的恢复结果。两组使用同一领域服务、同一 HTTP 工单协议和相同故障注入位置。

## 实验条件与复现

在 `backend` 目录运行 `python -m evaluation.compare_full_fault`。脚本与[原始结果](../evaluation/full_fault_comparison.json)一起保存；需要 Docker 引擎、已有 `redis:7-alpine` 镜像和已安装的后端依赖。每次创建唯一名称、随机本机端口的专用 Redis 容器，结束时仅停止此容器；两组分别使用临时 SQLite 案件库、独立进程 HTTP 模拟工单服务和唯一会话。Temporal 组另外启动本机内存型开发服务与独立 Worker。执行顺序是默认路径、Temporal 路径。

请求经 FastAPI `/chat`、真实 Redis 会话锁/状态/回执、物流 Agent 和领域服务。意图识别与路由固定为同一受控丢件案例，避免模型及向量检索波动。首轮创建待确认案件；第二轮“确认建单”让 HTTP 服务写入工单后主动断开连接。领域服务保存 `ticket_unknown`，脚本在最终回执写入前断言本地状态和远端工单数，然后停止 Redis，使该请求返回 503。随后在相同端口启动**空 Redis**，用原 `client_request_id` 重试，再用新 ID 发送“继续”触发核对。脚本断言两条路径最后均为 `ticket_created` 且远端始终只有一张工单。所有工单都是独立服务中的模拟数据。

## 本机实测

| 断言/观测 | 默认路径 | Temporal 路径 |
| --- | --- | --- |
| 初始案件 | `awaiting_confirmation` | `awaiting_confirmation` |
| 远端已落单、回执写入时 Redis 断线 | HTTP 503；本地 `ticket_unknown` | HTTP 503；本地 `ticket_unknown` |
| 空 Redis 恢复后，相同请求 ID 重试 | `ticket_created` | `ticket_unknown` |
| 新请求 ID 发送“继续” | `ticket_created` | `ticket_created` |
| 最终远端工单数 | 1 | 1 |
| 故障轮墙钟时间 | 3308.37 ms | 3865.59 ms |
| 原 ID 重试墙钟时间 | 364.01 ms | 31.82 ms |
| 新一轮核对墙钟时间 | 27.12 ms | 462.26 ms |

同 ID 差异符合代码执行语义：默认路径在 Redis 回执消失后重新进入领域服务，凭持久化案件和 Operation 键查询远端结果；Temporal 路径以同一请求 ID 获取已完成工作流的原结果，该结果形成于远端响应丢失时，仍是 `ticket_unknown`。因此 **Temporal 的同 ID 重取保证取回该轮结果，不保证随后自动刷新外部副作用状态**。新的“继续”轮次才会在两条路径中执行结果核对。不能把 200 `ticket_unknown` 解读为建单失败，也不能因此重发新的建单操作。

这一次样本中的三段延迟仅用于说明操作经过的路径，不用于性能优劣判定。两组顺序运行，Temporal 含本机开发服务和 Worker 的就绪影响，Windows Docker 端口转发、Redis 重启等待及 HTTP 断线超时均影响数值。此前[重复资源消融](DiAn-真实Redis断线与资源对照验收.md#重复延迟与资源消融)使用不同故障与存储条件，不能与本表拼接成趋势。

## 决策与边界

继续保持 `DIAN_TEMPORAL_LOGISTICS=false`。本轮没有发现 Temporal 在这个联合故障上的业务正确性优势；同 ID 原结果的语义反而要求客户端明确区分“重取当前轮”与“发起新的结果核对轮”。项目现有新轮次“继续”可以核对；如果要让同 ID 请求也返回最新案件状态，应先设计并验证独立的只读状态查询语义，避免改变请求回执的含义。

本轮未验证生产 Redis 故障切换、Redis 原数据恢复、Temporal 服务持久重启与 Redis 同时故障、真实承运商/工单系统、网络分区、多机并发或长时间负载。HTTP 服务是可按稳定 Operation 键查询的模拟实现；真实系统若不能按该键权威查询，就不能照搬“最多一张工单”的结论。下一步优先验证面向用户的未知状态及核对入口、后台未决 Operation 的超时告警和人工处理，并在同一故障矩阵下增加进程崩溃与并发样本；只有可靠性收益和成本门槛均满足，才重新评估默认执行引擎。
