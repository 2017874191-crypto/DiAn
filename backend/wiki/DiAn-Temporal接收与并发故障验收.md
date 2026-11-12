# DiAn Temporal 接收与并发故障验收

日期：2026-11-12。承接[Run/Step 与后台核对验收](DiAn-RunStep与后台核对验收.md)。目标是检查 Temporal 待投递记录到 Run 创建的可见性窗口，以及两个独立核对器、管理员挂起之间的并发边界。本次使用本机独立进程、真实 Temporal 开发服务和独立 HTTP 模拟工单；未接入真实承运商。

## 最小修复

`accept()` 现在在**同一个 SQLite 提交事务**里写 `temporal_dispatch` 与状态为 `accepted` 的 Run、`pending` 的派发 Step。派发后在同库更新为 `dispatched`；Activity 执行业务事务后，Run 转为等待输入、待确认或工单状态。Run 身份按用户、会话、客户端请求 ID 固定，客户端同 ID 换消息继续拒绝。API 仍可用原有 `GET /logistics/runs/{run_id}` 查询；默认 `DIAN_TEMPORAL_LOGISTICS=false`。

这一改动修复的是**本地接收记录与 Run 的原子可见性**，不改变 Temporal 自身的持久化保证，也不把非物流 Agent 接入 RunStore。旧版已落库但缺少 Run 信息的待投递行，在同 ID 再次 `accept()` 时补齐；升级时应先停旧 Worker 并处理旧待投递行，避免旧格式任务与新格式重放并行执行。

## 故障矩阵与原始断言

运行：

```powershell
cd backend
python -m pytest tests/test_temporal_logistics.py tests/test_logistics_http_operation.py -q
python -m pytest tests -q
```

| 注入点 / 对照 | 待投递 | Run / Step | Case、Operation、远端工单 |
| --- | --- | --- | --- |
| 消融：接收时跳过 Run 预留 | `pending` | Run 不存在；同 ID 再接收可补成 `accepted` | 尚无案件与工单 |
| 接收事务中、Run 预留前抛错 | 无记录，事务回滚 | 无记录 | 无案件与工单 |
| 接收事务提交后立即 `os._exit(88)` | `pending` | 新进程和 GET API 均查到 `accepted` / 派发待执行 | 启动 Worker 后同 ID 请求完成核验，尚未建单 |
| Workflow 已派发，Worker 尚未运行 | `dispatched` | `dispatched` / 派发成功 | 启动 Worker 后转为 `waiting_confirmation` |
| 两个独立核对器都已读到同一远端结果后同时提交 | 已有未决 Operation | 原 Run 最终 `completed` | Operation `succeeded`，远端仍 1 张工单 |
| 核对器已读到远端结果，管理员先挂起，再放行核对器 | 已有未决 Operation | Run `manual_review` | Operation `held`，保留远端编号；案件未被晚到结果改回成功，远端仍 1 张工单 |

测试用 `multiprocessing` 启两个独立核对进程，并用进程间事件固定竞争顺序；远端工单服务也是另一进程。Run 消融只关闭接收时的预留，证明缺失 Run 的可见性问题确由该步骤解决；它不比较两套完整执行引擎的性能。测试源分别在 `tests/test_temporal_logistics.py` 和 `tests/test_logistics_http_operation.py`，每项均可重跑。本机 Windows Python 3.11.9 的全量结果为 **227 项通过、1 条测试依赖弃用警告**，约 111 秒。这个墙钟时间包含 Temporal 开发服务及子进程启动，不能当作恢复 SLO；本轮未测 CPU、内存或吞吐。

## 复用边界与下一门槛

本地同库事务只保证接收表与 Run 一起提交；Workflow 启动在事务之后，由待投递扫描重试。Temporal 开发服务使用本机临时环境，本轮**没有**验证 Temporal 服务进程及持久存储重启。HTTP 模拟服务保证操作键唯一且 GET 权威；真实系统若没有这些契约，不得复用自动核对。管理员挂起阻止后续自动推进，但不能撤销已发生的远端写入。

当时的下一步是验证持久化 Temporal 服务重启、真实 Redis TTL、同案两次确认和取消边界。后续[持久服务重启与取消边界验收](DiAn-持久服务重启与取消边界验收.md)完成了本机开发服务持久重启、并发与取消实验；真实 Redis TTL、CPU/内存及运维成本仍未验收。只有业务结果和恢复收益在这些对照中成立，才讨论是否默认开启 Temporal；当前维持可选。
