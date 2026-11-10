# DiAn P0 开发与验收记录（2026-11-09 更新）

## 交付范围

依据 [DiAn 开发文档](DiAn开发文档.md) 的 P0 阶段，本次增加：

- `evaluation/logistics_eval_runner.py`：按案件回放 HTTP 多轮对话，检查案件状态、意图、角色、工具轨迹、会话工单数量和重复请求回执。
- `tests/test_logistics_eval_runner.py`：检查判分器对副作用、禁用工具、回执变化和会话隔离的处理。
- `scripts/accept_logistics_p0.py`：数据集校验、物流单测、API 契约测试及可选真实回放的一键入口。
- `docker-compose.acceptance.yml`：主机 Docker 上的隔离服务，使用独立数据卷和 `127.0.0.1:18000` 端口。
- `core/logistics.py`、`agents/agent_orchestrator.py`：修复明确否认丢件时误建案，以及已有案件中的确认疑问/否定被路由到通用客服的问题。
- `requirements-dev-lock-linux-py312.txt`：在 Linux x86_64 / Python 3.12 中解析的 112 个精确版本；这是版本快照，不含包文件哈希。
- `evaluation/prepare_logistics_review.py` 与 [测试集人工复核说明](DiAn测试集人工复核说明.md)：导出双人独立复核表，保护已填意见不被覆盖。

评测报告 JSON 包含总体状态计数、场景族计数、逐案逐轮失败原因。语义检查一律标记 `pending_human_review`，程序不会凭字符串匹配宣称语义通过。没有 `--restart-command` 时，重启用例标记 `unsupported`。模型或服务失败、环境失败、业务流程失败分别计数。

## 执行方式

先安装项目声明的依赖：

```powershell
$env:PYTHONUTF8='1'
python -m pip install -r requirements-dev.txt
python scripts/accept_logistics_p0.py
```

在主机 Docker 中启动独立验收环境并执行真实回放（会创建模拟案件与工单）：

```powershell
docker compose -p dian-p0-acceptance -f docker-compose.acceptance.yml -f docker-compose.local-check.yml up -d --build
python scripts/accept_logistics_p0.py --base-url http://127.0.0.1:18000 --db data/acceptance/logistics/cases.sqlite3 --restart-command 'docker compose -p dian-p0-acceptance -f docker-compose.acceptance.yml -f docker-compose.local-check.yml restart dian' --report data/acceptance/eval/p0-final.json
```

锁定环境的离线复核：

```powershell
docker run --rm --user 0 -v "${PWD}:/app" -w /app --entrypoint sh dian-ablation:local -c 'pip install -q -r requirements-dev-lock-linux-py312.txt && pip check && python scripts/accept_logistics_p0.py'
```

该配置使用已有 `.env` 中的模型凭据调用真实模型，Redis/ChromaDB 使用独立卷，SQLite 和报告位于忽略提交的 `data/acceptance/`。`--db` 必须与运行中的 API 实际使用的 SQLite 文件相同。只对隔离的演示环境运行，勿将生产数据库传给回放器。开启鉴权时额外传入 `--token`，其身份应映射为 `demo-user`。`--split test` 的冻结集可通过独立 runner 命令评测，开发迭代优先使用 dev 集。

重启用例需额外传入 `--restart-command`，例如仅重启隔离测试项目的服务命令。回放器会等待 `/health` 恢复；不提供该参数则将重启案件列为未执行。此命令由操作者明确指定，不预设生产服务。

## 本次已观测结果

| 检查 | 结果 | 限制 |
| --- | --- | --- |
| 数据集结构 | 50 案、73 轮、24 场景族；结构有效 | 未提供上游源文件缓存，因此 `source_record_check=false` |
| 一键验收入口（2026-11-09 复核） | 数据集校验通过，27 项测试通过 | Linux/Python 3.12 容器，112 个精确版本安装成功且 `pip check` 无冲突；本次为离线阶段 |
| 首次环境问题 | 缺 `prometheus_client`，API 测试无法收集 | 已按 `requirements-dev.txt` 安装并复跑通过；安装发生在本机当前系统 Python 环境 |
| 主机 Docker 隔离服务 | 后端、Redis、ChromaDB 均 healthy | 使用项目名 `dian-p0-acceptance`，未对已有 `dian-*` 服务发评测请求 |
| dev 集 HTTP 回放 | 30/30 案流程断言通过 | 使用真实模型和模拟业务工具；语义检查仍待人工复核；报告见 `data/acceptance/eval/p0-final.json` |
| 重启恢复 | `DIAN-046` 三轮通过 | 仅重启隔离后端容器，验证现有案件和工单保留；不代表跨主机灾备 |
| 失败修复前基线 | 26 案流程通过、3 案流程失败、1 案重启未执行 | 失败为 `DIAN-024`、`DIAN-036`、`DIAN-047`；修复后重跑全部 dev 集 |
| VMware 虚拟机（2026-11-09 复核） | VMware Workstation 和 `Ubuntu bb` 虚拟机已找到且正在运行 | 来宾为 Ubuntu，当前 Python 3.8.10；未找到 Docker、Redis 或 DiAn 项目目录，可用空间约 5.6 GB，尚不具备本项目真实回放前提 |
| 人工金标复核 | 未执行 | 当前数据集金标状态仍为 `agent_curated_requires_human_review` |
| 冻结集复核材料 | 已生成 20 案、28 轮空白复核表 | `data/acceptance/eval/test-review.csv`；尚无复核者签名和模型语义判定 |

## P0 完成门槛与剩余工作

P0 的程序流程验收已通过，但整体 P0 尚有以下工作：

1. 已在项目 Python 3.12 容器用精确版本快照复核离线测试；如需更强的软件供应链锁定，还应补包文件哈希与受信下载源。
2. 冻结测试集需两位真实复核者独立填写金标意见，再记录一致性与裁决；任何根据测试集调参的用例都应移入回归集，并重建未暴露测试集。
3. 当前 30/30 dev 报告生成于扩展 `observed` 字段之前，流程结论有效，但不包含逐轮实际回复；语义复核需要后续新回放报告。
4. 在独立 DiAn 仓库保存正式基线提交、来源记录和迁移说明。

## 对抗性验收关注点

- 同一 `client_request_id` 复用：必须返回相同 `request_id` 和工单数。
- 不同请求重复确认：工单总数仍为 1。
- 缺字段、订单归属错误、订单/运单冲突：不创建工单。
- 查询超时和物流事实冲突：进入人工复核，不伪造事实或责任结论。
- 换订单、换会话、换用户：案件和工单不串用。
- 用户声明有材料：仍需与真实文件证据区分。

## 消融实验协议（待真实回放）

按相同数据集、相同模拟工具世界和相同模型版本分别执行：完整方案、关闭意图 Embedding 分支、关闭 Pattern 分支、取消多轮状态继承（仅在独立实验分支或可恢复测试环境中）。每组至少记录各场景族的流程通过率、错误路由、错误建单、漏建单、平均耗时和模型调用失败数；与完整方案逐案件配对比较。不得把服务失败样本剔除后直接宣称提升，也不得用测试集反复调参。若某机制关闭后没有可测的收益下降，应考虑删除或简化该机制。当前未执行实验，因此本文档不声称三路融合或记忆机制对 DiAn 案件质量已有量化增益。
