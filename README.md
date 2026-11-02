# DiAn 物流异常协同台

DiAn 是面向疑似丢件、签收争议、破损错发的客服 Agent 项目，提供多轮信息收集、物流核验、案件状态管理、用户确认、模拟工单和前端案件卡片。

当前物流与工单均为模拟服务，材料举证仅记录用户声明，不会执行真实赔偿、退款或承运商操作。物流案件的核验顺序与写操作由确定性代码控制，模型用于意图识别和原有客服角色，不把当前物流流程宣称为自由规划的多 Agent 并行协作。

## 目录

```text
backend/                 Python / FastAPI 后端（发行包 dian-backend）
  agents/                 客服角色与路由
  core/logistics.py       物流案件、事务与幂等工单
  config/logistics/       模拟订单、物流、规则和知识文档
  evaluation/datasets/    带来源的案件级评测集
  skills/                 角色处理规范
  tests/                  回归与接口测试
  wiki/                   架构、实现与验收文档
frontend/                Vue 3 / Vite 前端（npm 包 dian-frontend）
```

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

## 边界与部署

密钥、运行数据库、日志和依赖目录不入库。正式部署需启用生产 Token 鉴权、HTTPS、备份，并接入真实业务系统及文件证据处理。当前前端案件刷新恢复不包含历史聊天列表恢复。

## 来源与许可

项目在 DiAn 后端与前端基础上扩展，来源及基线版本见 [NOTICE.md](NOTICE.md)。评测数据的上游许可和改写说明位于数据集目录；未将上游数据统一重标为 MIT。本仓库没有新增一份覆盖所有代码的统一许可证。
