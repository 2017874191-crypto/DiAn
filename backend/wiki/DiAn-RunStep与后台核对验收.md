# DiAn 物流 Run/Step 与后台结果核对验收

日期：2026-11-12。承接[独立 HTTP 工单与 Operation 验收](DiAn-HTTP工单Operation验收.md)，本阶段解决运行过程不可按身份查询、未决外部结果只能靠用户再次发消息核对的问题。默认模拟工单模式及 Temporal 可选开关保持原值。

## 对象、状态与事务边界

- 每个物流用户轮次在案件库中保存一个 `logistics_runs` 记录。运行 ID 由用户、会话和客户端请求 ID 确定；没有客户端请求 ID 的直接领域调用则使用服务端请求 ID 与消息摘要。同一运行 ID 不得绑定不同消息。`logistics_steps` 记录案件裁决、工单结果两个固定步骤。案件快照只暴露最近一次运行 ID。
- 案件与 Run/Step 在同一短事务提交。`awaiting_info` / `awaiting_evidence` 对应 `waiting_input`，`awaiting_confirmation` 对应 `waiting_confirmation`，`ticket_pending` 对应 `reconciling`，`ticket_unknown` 对应 `outcome_unknown`，建单完成对应 `completed`。外部结果提交时，在同一事务内更新 Operation、Case 和相关未决 Run/Step。
- `GET /logistics/runs/{run_id}?user_id=...&conv_id=...` 查询运行和步骤。配置 API Token 后普通用户仅可查自身，跨会话或不存在均返回 404；管理员待处理列表 `GET /logistics/operations/pending` 含未决及已挂起操作、结果年龄和人工原因。列表仅供管理员，不展示完整建单参数。
- 管理员可 `POST /logistics/operations/{op_key}/hold`，提交至少 10 字的 `reason`，记录管理员身份和时间。挂起与案件转人工在同一事务完成；后台扫描及用户“继续”都不得再自动建单。若远端调用恰在挂起时返回，已观察到的工单号仍记在 Operation 供人工核实。挂起不撤销远端操作，也不代表远端未建单。

## 后台核对与重试边界

显式设置 `DIAN_TICKET_PROVIDER=http_mock` 后，可另起进程运行：

```powershell
cd backend
$env:DIAN_TICKET_PROVIDER="http_mock"
$env:DIAN_TICKET_URL="http://127.0.0.1:18191"
$env:DIAN_LOGISTICS_DB="data/logistics/cases-http.sqlite3"
python -m core.logistics_reconcile
```

API、核对器与模拟工单服务必须使用同一配置和持久目录。核对器默认每 10 秒扫描，且只处理至少 10 秒未更新的 `prepared` / `unknown`；`DIAN_TICKET_RECONCILE_INTERVAL` 可调扫描间隔。它只调用远端 **GET**，匹配原 Operation 的键和参数哈希后才提交成功。404 时保留原状态，远端不可用或结果冲突时保留待核对，并输出扫描数、成功数、未决数及最老未决年龄。它不调用 POST，也不代替用户重新确认。用户新一轮“继续”沿原有领域路径核对并在权威 404 时按固定键创建；同一 `client_request_id` 的 API 回执重放不会触发新一轮核对。

取消语义：本阶段没有可安全撤销远端建单的取消 API；管理员“挂起”仅停止后续自动动作，已发生的外部副作用必须人工核实。等待用户输入和确认时不占用后台 Worker。Run 的 `completed` 指本轮物流动作及已核对结果完成，不表示赔付、承运商处理或人工流程完成。

## 验收与消融

运行 `python -m pytest tests/test_logistics_http_operation.py -q`，以及 `python -m pytest tests -q`。关键断言：

| 场景 | 验收点 |
| --- | --- |
| 远端已建单但响应断开 | 原 Run 从 `outcome_unknown` 经后台 GET 收敛为 `completed`，Step 成功，远端仍一张工单 |
| 远端 404 | 后台不发 POST，Operation 仍未决，远端工单数为 0 |
| 管理员挂起 | 普通用户调用被拒，原因可在管理员列表读取；后台扫描与用户“继续”均不能建单 |
| 用户与会话隔离 | 运行查询必须同时匹配用户和会话，配置 Token 后跨用户请求被拒 |
| 既有路径回归 | 原 SQLite 模式、可选 Temporal 路径及 HTTP 故障恢复继续通过 |

消融比较同一条已由远端创建但响应丢失的 Operation：不运行后台核对时保持 `outcome_unknown`；启动核对后，凭 GET 收敛为 `completed` 且工单数不变。它证明自动收敛能力来自扫描，不证明真实 Provider 的查询契约。404 测试是安全边界的反例检查，不作为“自动重建”方案的性能比较。本机 Windows Python 3.11.9：后端全量 222 项通过、1 条测试依赖弃用警告；最后的挂起状态调整后，相关 12 项定向测试通过。没有运行生产负载或真实承运商实验。

## 明确未完成的部分

这不是通用 RunStore。当前 Run 从物流领域事务提交开始：Temporal 的 `temporal_dispatch` 已接受、但 Worker 尚未执行时，Run 查询仍可能 404，需另查原待投递表。没有统一的任务领取租约、跨进程取消、人工结案动作、持久监控时序、真实工单鉴权与证据处理。管理员挂起是止损动作，不是确认工单不存在。真实 Provider 只有在查询权威性、稳定键幂等、归属校验均有明确契约后才能复用自动核对；否则保留人工处理。

下一阶段应先测接受到 Run 创建之间的崩溃窗口及并发扫描/挂起竞态，再决定是否统一 Temporal 待投递表与 Run 身份、引入租约及告警；只有在故障和维护成本对照成立时才考虑默认开启 Temporal。
