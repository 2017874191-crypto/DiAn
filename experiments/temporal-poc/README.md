# DiAn Temporal 最小接入验证

此目录是**隔离实验**，没有接入现有 FastAPI 入口，也没有替换当前物流业务流程。目的是验证 Temporal 对执行恢复的贡献，以及外部副作用仍需业务 Operation 记录、稳定幂等键与结果查询的事实。实验只使用独立的模拟 HTTP 工单服务；不调用真实模型或工单平台。

## 组成与责任

- `flow.py`：Temporal Workflow，仅编排 Activity；不直接进行 I/O。
- `services.py`：可被强制终止的 Worker 和独立的 HTTP 模拟工单服务。后者在 PostgreSQL 中保存真实创建次数，并可在创建成功后断开连接。
- `store.py`：业务数据库。保存请求去重、案件归属、Operation 状态、模拟工单与一次性故障标记。Temporal 不管理这些业务表。
- `verify.py`：启动 Temporal 开发服务、HTTP Provider、多个 Worker，执行故障场景，并对每个 Workflow 的历史做确定性重放检查。
- `acceptance.json`：本机实际运行的机器可读验收记录。

对照组 `engine` 每次 Activity 重试使用新 Provider 键，特意暴露“引擎恢复 ≠ 外部恰好执行一次”。`full` 组先准备 Operation；每次调用前按稳定业务键查询 Provider；确认已有结果后写回 `succeeded`。`unknown` 表示调用结果尚不明确，不能当成失败或安全重试的依据。

## 本地重跑

需要 Python 3.11+、可访问的 Temporal 开发服务、**独立测试数据库** PostgreSQL。项目正式运行环境的 Python 版本仍以仓库要求为准。执行位置为本目录。

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

一种可选启动方式为 `docker compose up -d`，端口仅绑定 `127.0.0.1`。`verify.py` 会自行启动 SDK 提供的 Temporal 本地开发服务，因此 Docker 中的 Temporal 仅供手动观察；数据库须在本机 `15439` 端口，默认 DSN 可用 `POC_DSN` 覆盖。不要让实验脚本指向共享生产数据库。

当前主机 Docker Desktop 因 Secrets Engine socket 错误退出，实际验收使用独立的本地 PostgreSQL 13 数据目录 `data/pg`，没有改动主机原有 PostgreSQL 服务，也没有重置 Docker。独立数据库可参考以下步骤；只对专用数据目录执行：

```powershell
& D:/postgresql/bin/initdb.exe -D ./data/pg -A trust -U poc --encoding=UTF8
Start-Process -FilePath 'D:/postgresql/bin/postgres.exe' -ArgumentList '-D','./data/pg','-p','15439','-h','127.0.0.1' -WorkingDirectory (Get-Location).Path -WindowStyle Hidden -RedirectStandardError './data/postgres-stderr.log'
& D:/postgresql/bin/createdb.exe -h 127.0.0.1 -p 15439 -U poc poc
./.venv/Scripts/python.exe -u verify.py
```

本地 `trust` 认证与容器内固定口令**只适用于回环地址上的一次性实验**。开发服务的 Temporal 历史在测试进程结束后消失；`acceptance.json` 保留结果摘要，不能充当长期可审计执行历史。重跑会生成新的随机请求 ID，不覆盖旧的业务数据。

## 验收口径与结果

2026-11-11 本机验收：Temporal Python SDK 1.36.0；SDK 拉起的 Temporal CLI 1.10.0 / Server 1.32.2；独立 PostgreSQL 13.23；Windows Python 3.11.9。6 个场景均通过，详见 `acceptance.json`。测试中 `os._exit(86)` 直接终止首个 Worker，第二个进程接管同一 Task Queue；`drop` 则在 Provider 已保存工单后直接断开连接。

| 组别 | 故障点 | 实际工单数 | 证明范围 |
| --- | --- | ---: | --- |
| 仅引擎 | 外部成功、Worker 报告前崩溃 | 2 | 引擎恢复会重试 Activity，单靠它无法防止重复副作用 |
| 完整最小方案 | 无故障 | 1 | 正常提交 |
| 完整最小方案 | 调用前崩溃 | 1 | 新 Worker 继续执行 |
| 完整最小方案 | 外部成功、记录结果前崩溃 | 1 | 按业务键查询到既有工单 |
| 完整最小方案 | Operation 已提交、引擎确认前崩溃 | 1 | 重试读取已提交结果 |
| 完整最小方案 | 外部成功但响应丢失 | 1 | 重试前查询，避免第二次创建 |

每组还验证同一请求 ID 和相同输入映射同一 Workflow ID，输入变更被拒绝，Temporal 不接受重复启动；完整方案进一步检查跨 Workflow 的同一业务操作只保留一张工单、案件归属冲突被拒绝、Operation 结束为 `succeeded`，以及已完成 Workflow 的历史可重放。

## 能力边界与下一步

此实验**不能证明 DiAn 主业务链路已接入 Temporal**。`confirmed=True` 只是固定的测试输入，不等于现有系统的用户身份验证、确认版本或参数绑定。`requests` 表到 Temporal 启动之间仍存在双写窗口，尚未加入待投递恢复。没有完成真实工单适配、Redis 过期后的案件查询、同案并发版本控制、取消后结果核对、依赖宕机预算、生产级服务配置或升级期间的旧历史兼容测试。模拟 Provider 支持稳定键和结果查询；真实服务不支持时不能外推“恰好一次”。

决定接入主链路前，应先将身份与确认校验绑定案件版本及操作参数；实现可靠待投递；将真实 Provider 改成短事务准备、远程调用、短事务核对；加入同案并发和取消故障实验。届时再比较当前基线、仅引擎和完整方案的恢复率、重复建单数、耗时与维护成本。
