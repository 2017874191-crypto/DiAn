# DiAn 独立 HTTP 工单与 Operation 验收

日期：2026-11-11。目标是验证跨服务副作用窗口：本地保存建单意图后，独立工单服务可能已创建工单，但响应丢失或调用进程崩溃。该验证沿用原有物流案件状态机和可选 Temporal 主链路，不接入真实承运商或赔付系统。

## 机制与取舍

默认 `DIAN_TICKET_PROVIDER=sqlite`，保持原有案件和模拟工单在同一 SQLite 事务的演示路径。显式设为 `http_mock` 后，使用另一个进程及其独立 SQLite 数据库提供 `GET /tickets/{key}` 与 `POST /tickets`。服务端用 `op_key` 唯一约束保证同一操作键只对应一张工单；同键不同参数返回冲突。

在确认建单的短事务里，领域服务核验案件状态、规则版本和明确确认，保存 `ticket_operations`：稳定业务键、案件和会话归属、确认请求 ID、案件版本、固定参数及其哈希、操作状态。事务提交后才访问外部 HTTP 服务。执行时先按键查询；明确不存在才发送创建请求。返回结果必须匹配已保存参数哈希。随后在短事务里提交 `succeeded` 与案件工单号。超时、断线、服务错误和结果不匹配均记为 `unknown`，绝不当作“肯定未建单”。再次继续时先查询同一键，不生成新键。

案件新增 `ticket_pending` 和 `ticket_unknown` 状态，可由 `/chat` 与案件卡片看到。页面“核对模拟工单结果”发送新一轮“继续”；每轮使用新的 `client_request_id`。同一个请求 ID 在 API 回执层会返回原回执，不能作为主动核对命令。独立 HTTP 模式把确认操作标为可安全重入，因为 Operation 先持久化；原 SQLite 模式的旧标记逻辑保持不变。HTTP I/O 在线程执行，避免阻塞 API 事件循环；Temporal Worker 也调用相同领域服务和 Operation 账本。一次远端查询和创建的超时各为 3 秒，总时长仍受上游请求预算约束。

`case_version` 是确认时的案件时间戳，`confirmation_id` 是确认请求 ID，二者同时固定在案件快照和 Operation 中；`params_hash` 固定实际建单参数。调用前及结果落库时均再次比对，后续参数或确认依据漂移会阻止结果写成成功并进入待核对；若已观察到远端工单号，会保留在 Operation 供人工处理。这里没有实现完整的审批凭证、分布式锁或租约。主链路仍为同步请求驱动，`unknown` 目前需要用户新一轮“继续”触发核对，尚无后台扫描和告警。

## 本机复现

在 `backend` 安装依赖后，两个终端分别运行。使用独立的本地数据库路径，以免与默认演示数据混用。

终端 A（独立模拟工单服务，PowerShell）：

```powershell
$env:DIAN_TICKET_MOCK_DB="data/logistics/mock-http-tickets.sqlite3"
$env:DIAN_TICKET_MOCK_DROP_ONCE="true"
python -m core.mock_ticket_http
```

终端 B（API）：

```powershell
$env:DIAN_LOGISTICS_DB="data/logistics/cases-http.sqlite3"
$env:DIAN_TICKET_PROVIDER="http_mock"
$env:DIAN_TICKET_URL="http://127.0.0.1:18191"
$env:DIAN_TEMPORAL_LOGISTICS="false"
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

在同一用户、会话中发送“包裹疑似丢件，订单号 DEMO1001”，下一轮发“确认建单”。故障开关会在远端保存首张工单后断开连接，本地显示 `ticket_unknown`。再发“继续”，应查询到原工单并显示 `ticket_created`。测试程序会自动启独立服务、模拟断线及进程崩溃；不需要 Docker。若要验证 Temporal 路径，按[主链路接入验收](DiAn-Temporal主链路接入验收.md)启动服务与 Worker，并在 API、Worker 中设置同一组工单环境变量。切换 Provider 前，应先处理完未决 Operation；运行中切换模式不在本次验收范围。

## 验收断言

运行 `python -m pytest tests/test_logistics_http_operation.py -q`：

| 场景 | 实际断言 |
| --- | --- |
| 远端提交成功但断开连接 | 首轮为 `ticket_unknown`，本地 Operation 为 `unknown`，远端工单数为 1；重启本地服务、清空会话缓存后“继续”变为 `ticket_created`，工单数仍为 1 |
| 远端不可用 | 首轮为 `ticket_unknown`；服务恢复后核对同一键并创建，远端工单数为 1 |
| 远端提交后调用进程退出 | 新进程看到本地 `prepared`，按键查询并恢复 `ticket_created`，远端工单数仍为 1 |
| 远端调用期间案件参数变化 | 远端已有一张工单，但本地案件和 Operation 均保持 `unknown`；已知远端工单号留在 Operation，不把旧参数的结果记作当前案件成功 |
| `/chat` 入口 | 三轮状态依次为 `awaiting_confirmation`、`ticket_unknown`、`ticket_created`；HTTP 模式未写旧式禁止重试标记 |
| Temporal 可选路径 | 相同 Operation 账本完成断线后的查询恢复，远端工单数为 1 |

消融对照也使用相同的独立模拟服务和相同参数：重复发送**固定键**，远端计数仍为 1；故意去掉稳定键、重试时发送**新键**，计数变为 2。该对照只证明稳定键配合远端唯一约束的必要性；它没有把 Operation 账本、先查询、远端幂等各自的贡献完全分离。结果核对能力由上面的断线和崩溃测试验证。

本机 Windows Python 3.11.9：上述 6 项及 1 项消融对照通过；全量后端 217 项通过、1 条测试依赖弃用警告；前端 17 项通过，`npm run build` 成功。测试证明的是**约定范围内同一稳定键最多一张模拟工单**，不证明跨案件去重、真实承运商幂等、生产可用性或并发压力指标。

## 能力边界与下一步

独立模拟服务没有真实身份认证、TLS、审计权限和运营后台。`GET` 的 404 必须是对该操作键的权威“不存在”；若真实系统查询是最终一致或不支持稳定键，就不能照搬“404 后创建”。真实 Provider 接入前必须确认同键同参数的幂等语义、结果查询时效、冲突处理和服务端归属校验；拿不到这些契约时，未知结果应停在人工核验，不得自动重试。

下一步：补 Run/Step 的持久身份与查询 API，明确待输入、等待核对、取消和重试语义；增加后台扫描未决 Operation、年龄指标与人工处理入口；再做进程终止点、并发、超时与服务恢复的矩阵实验，并保存原始结果。Temporal 仍保持可选，是否成为默认执行引擎取决于这些实验和维护成本，而非单次成功演示。
