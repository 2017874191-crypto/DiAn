# DiAn 物流异常协同台

DiAn 是面向疑似丢件、签收争议、破损错发的客服 Agent 项目，提供多轮信息收集、物流核验、案件状态管理、用户确认、模拟工单和前端案件卡片。

当前物流与工单均为模拟服务，材料举证仅记录用户声明，不会执行真实赔偿、退款或承运商操作。物流案件的核验顺序与写操作由确定性代码控制，模型用于意图识别和原有客服角色，不把当前物流流程宣称为自由规划的多 Agent 并行协作。

## 项目定位与核心工作

DiAn 是独立维护的物流异常领域项目。项目复用通用客服 Agent 的基础能力，并围绕物流案件重新设计领域模型、写操作边界和验证体系。核心工作包括：

- 设计疑似丢件、签收争议、破损错发的案件状态机，由领域代码裁决状态迁移。
- 增加订单、物流、工单和证据 Provider 接口，开发环境使用可重复的模拟工具世界。
- 实现“先核验、再展示方案、下一轮明确确认”的建单门槛，以及请求重放、并发确认和服务重启后的幂等恢复。
- 建立 50 个案件、73 个用户轮次的案件级数据集，校验案件状态、工具轨迹、禁止动作、工单数量和身份连续性。
- 对规则、LLM 与向量识别分支进行消融实验，并保留失败样本、修复依据和能力边界。

底层框架来源和基线版本集中记录在 [NOTICE.md](NOTICE.md)，DiAn 的领域设计、实现、前端和验收材料在本仓库独立维护。

## 目录

```text
backend/                 Python / FastAPI 后端（发行包 dian-backend）
  agents/                 客服角色与路由
  core/logistics.py       物流案件、持久 Operation 与模拟工单
  config/logistics/       模拟订单、物流、规则和知识文档
  evaluation/datasets/    带来源的案件级评测集
  skills/                 角色处理规范
  tests/                  回归与接口测试
  wiki/                   架构、实现与验收文档
frontend/                Vue 3 / Vite 前端（npm 包 dian-frontend）
experiments/temporal-poc/  Temporal 执行恢复与副作用核对的隔离验证
```

[Temporal 最小接入验证与验收记录](experiments/temporal-poc/README.md)：早期隔离实验，使用真实 Temporal 开发服务、独立模拟 HTTP 工单服务及进程崩溃实验。后续已完成可选主链路接入。

[Temporal 物流主链路可选接入与验收](backend/wiki/DiAn-Temporal主链路接入验收.md)：`/chat` 的物流执行可切换为持久化待投递与独立 Worker，默认仍使用原路径。

[Temporal 主链路对照实验](backend/wiki/DiAn-Temporal主链路对照实验报告.md)：相同物流案件比较原路径与 Temporal 路径的结果、延迟和故障恢复。

[案件关联持久化验收](backend/wiki/DiAn-案件关联持久化验收.md)：Redis 案件指针丢失后，从业务库恢复当前案件与后续确认，并验证用户和会话隔离。

[独立 HTTP 工单与 Operation 验收](backend/wiki/DiAn-HTTP工单Operation验收.md)：可选的跨进程模拟工单服务、持久 Operation、断线及进程崩溃后的结果核对；默认仍为同库模拟工单。

[物流 Run/Step 与后台结果核对验收](backend/wiki/DiAn-RunStep与后台核对验收.md)：按用户和会话查询运行步骤；可选后台只读核对未决工单，管理员可挂起操作并记录原因。

[Temporal 接收与并发故障验收](backend/wiki/DiAn-Temporal接收与并发故障验收.md)：接收记录与 Run 同事务创建，验证进程退出、派发等待及多进程核对与挂起竞态。

[持久服务重启与取消边界验收](backend/wiki/DiAn-持久服务重启与取消边界验收.md)：受控验证 Temporal 开发服务持久重启、同案并发确认及取消等待后远端操作仍可完成；另用独立 Docker Redis 验证回执和状态 TTL 后案件仍可回查。

[真实 Redis 断线与资源对照验收](backend/wiki/DiAn-真实Redis断线与资源对照验收.md)：隔离 Redis 容器故障、跨进程会话锁、Temporal 与 Worker 联合重启，以及三轮延迟和进程资源对照。

## 快速启动

需要 Docker 与 Docker Compose v2。前端构建需要 Node.js 22 或更高的兼容版本；宿主机运行后端需要 Python 3.12。模型通过服务端 API 调用，无需 GPU。

先构建前端：

```shell
cd frontend
npm ci
npm run build
cd ../backend
```

复制 `.env.example` 为 `.env`，填写模型配置。PowerShell 使用 `Copy-Item .env.example .env`；Linux/macOS 使用 `cp .env.example .env`。

```dotenv
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_API_KEY=填写你自己的API密钥
ANTHROPIC_MODEL=填写账号支持的模型ID
DIAN_ENV=development
```

Linux 宿主机需预建并授权持久化目录给容器用户 UID 1000，例如：

```shell
mkdir -p data/chroma data/eval data/logistics logs
sudo chown -R 1000:1000 data logs
```

从 backend 目录启动：

```shell
docker compose -f docker-compose.yml -f docker-compose.frontend-local.yml up -d --build
```

- 前端：http://localhost:5174
- API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health
- Prometheus：http://localhost:9090

首页点击“体验物流异常案件”可明确切换为 `demo-user`，并填入演示问题。也可发送“包裹疑似丢件，订单号 DEMO1001”，在同一会话确认建单。

| 模拟订单 | 演示用途 |
|---|---|
| DEMO1001 | 疑似丢件 |
| DEMO1002 | 显示签收但未收到 |
| DEMO1003 | 破损错发与材料声明 |
| DEMO1004 | 物流查询超时 |

上述命令依赖能访问 Docker Hub、PyPI 和 Chroma 模型下载源。`docker-compose.local-check.yml` 是本机镜像拉取受限时的替代配置，需要先准备对应应用镜像。`docker-compose.web.yml` 和前端独立 Compose 中的 Java 全栈路径属于原项目可选部署，需要额外的 Java 项目，本仓库没有附带该项目。

## 评测与验证

案件级数据集位于 [dian_logistics_v1](backend/evaluation/datasets/dian_logistics_v1/README.md)：50 个中文案件、73 个用户轮次，40 条由三份开源数据中的真实记录改写，10 条为自建工程用例。包含来源定位、许可、工具世界、业务预期、开发/测试划分和离线验证器。

```shell
python backend/evaluation/datasets/dian_logistics_v1/validate_dataset.py
cd frontend
npm test
```

后端测试需要安装 `backend/requirements.txt` 和 `backend/requirements-dev.txt`。案件级数据尚未接入原有 `/eval/run`，尚未运行批量模型评分，金标待人工复核。已有测试数量和本地联调结果见 wiki 中的实施与验收记录，不代表生产 SLA。

面试版的开发基线、消融实验与故障追踪证据见：

- [v0.1.0 面试版说明与冻结验收](backend/wiki/DiAn-v0.1.0面试版说明.md)
- [Agent Infra 工程能力审查与优化方案](backend/wiki/DiAn-AgentInfra工程能力审查与优化方案.md)（2026-11-11，含能力分级、代码证据、故障复现、消融设计与分阶段验收）
- [DiAn 开发文档](backend/wiki/DiAn开发文档.md)
- [规则与消融验收记录](backend/wiki/DiAn-P2规则与消融验收记录.md)
- [故障追踪与物流向量开关验收记录](backend/wiki/DiAn-P3故障追踪与物流向量开关验收记录.md)

## 边界与部署

密钥、运行数据库、日志和依赖目录不入库。正式部署需启用生产 Token 鉴权、HTTPS、备份，并接入真实业务系统及文件证据处理。当前前端案件刷新恢复不包含历史聊天列表恢复。

## 来源与许可

DiAn 复用了 EchoMind 与 EchoMindFrontend 的通用客服基础，来源及基线版本见 [NOTICE.md](NOTICE.md)。评测数据的上游许可和改写说明位于数据集目录；未将上游数据统一重标为 MIT。本仓库没有新增一份覆盖所有代码的统一许可证。
