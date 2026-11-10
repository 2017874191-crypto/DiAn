# DiAn 规则对齐与意图消融验收记录

日期：2026-11-10。范围：物流异常案件演示版及 `dian_logistics_v1` 开发集；没有接入真实订单、承运商、工单或赔付系统。

## 1. 变更与理由

| 项目 | 本轮决定与实现 |
| --- | --- |
| 签收争议 | 轨迹已签收、用户称未收到：核验后展示模拟争议核查方案，用户下一轮明确确认才建模拟工单；轨迹尚未签收却提出签收争议：人工核查。同步修正开发文档，均不认定责任。 |
| 建单授权 | `core/logistics.py` 要求案件原状态为 `awaiting_confirmation`、规则版本一致，再接受本轮精确的“确认建单”等文案。首次确认、疑问、否定、材料未声明、人工核查状态均不能直接建单。 |
| 意图否定 | 否定“丢件／破损／错发”不能作为异常建案依据；同句其他肯定异常仍可识别。 |
| 验收断言 | HTTP 回放对比案件快照、工具轨迹和工单数量，并额外检查同案续接、请求重放、服务重启后的案件号和已有工单号。 |
| 实验开关 | `INTENT_RECOGNITION_MODE` 支持 `rules_only`、`llm_rules`、`full`；领域事实核验、确认与写操作规则三组相同。纯规则组的非物流未命中请求给固定澄清回复，不调用通用客服模型。 |

## 2. 验证环境和数据

- 使用主机 Docker 的独立 Compose 项目，三组按顺序运行，各自拥有独立 Redis、Chroma 卷及 `data/ablation-verified/<variant>` 案件数据库。验收后已停止这些容器；未删除卷与报告，未操作原有项目容器。
- 三组使用同一镜像 `dian-ablation:local`，镜像 ID：`sha256:f388aa6aa69535f1eff985418ac25726a4e6503e4135b484bd802722f6624e0c`。`full` 的向量分支使用本地 n-gram，不调用外部向量服务。
- 开发集 30 案，`dev.jsonl` SHA-256：`bfe297d2044b343f4ce5d3961eaed5b37883710bc42fc2a604f951322602bbf4`。数据集结构校验通过：总计 50 案、73 轮、24 场景族；`source_record_check=false`，本次未带上游源记录缓存做来源复核。冻结 `test` 集 20 案未用于本轮调试或评分。
- 物流单测、API 契约和 runner 测试：`34 passed`。数据集结构校验：`valid=true`。`DIAN-045` 重放及重复确认、`DIAN-046` 受控重启在模型＋规则组均通过，案件号、工单号保持一致。首次直接确认的单元测试验证未建单。

## 3. 消融结果（首次完整回放）

| 指标 | 规则＋状态机 `rules_only` | LLM＋规则 `llm_rules` | 三路完整方案 `full` |
| --- | ---: | ---: | ---: |
| 流程断言通过 / 30 案 | 21 | 30 | 29 |
| 失败类型 | 9 案流程未通过 | 无 | 1 案模型或服务失败 |
| 意外模拟工单 | 0 | 0 | 0 |
| 已观察轮次 | 44 | 45 | 44 |
| HTTP 往返耗时 P50 / P95 | 187 / 250 ms | 1515 / 5953 ms | 1593 / 6000 ms |
| 记录到的模型调用 / 错误 | 0 / 0 | 59 / 0 | 54 / 1 |
| 服务方报告的输入 / 输出 token | 0 / 0 | 11193 / 3757 | 9188 / 3340 |

口径：流程通过要求预期案件状态、意图、Agent、工具、工单数量、快照及身份连续性断言全部通过；自然语言回复的语义检查仍是 `pending_human_review`。P50/P95 取已观察轮次的客户端 HTTP 往返，包含请求处理，不含启动与重启等待。模型调用和 token 来自每次响应的 `performance.llm`；失败请求可能没有完整用量，完整组只记录到 44 轮，故三组 token 总数不能当作同一工作量的单价比较。供应商价格未冻结，货币费用未估算。

规则组失败的九案：`DIAN-001`—`006`、`017`、`018`、`025`。它们主要是较委婉的破损、签收争议、疑似丢件表达，未匹配领域关键词，进入固定澄清路径。模型＋规则组全部补足。完整组三路方案没有新增通过案件；`DIAN-029` 的非物流发运状态查询首次返回 `agent_failed`，按模型／服务失败计入 29/30。保留原始报告后，同案独立复测通过 1/1；故障尚未定位，不能据此认定完整方案存在稳定的语义缺陷，也不能把首次失败改写为通过。

原始报告：`data/ablation-verified/rules_only/eval/report.json`、`data/ablation-verified/llm_rules/eval/report.json`、`data/ablation-verified/full/eval/report.json`；单案复测：`data/ablation-verified/full/eval/DIAN-029-retry.json`；逐案比较：`data/ablation-verified/comparison.json`。

## 4. 结论和下一步

在这 30 个开发案件上，LLM＋规则比纯规则多通过 9 案；完整三路没有观察到增益，首次运行有一次未定位的服务失败，P50/P95 也略高。物流写操作继续只由领域状态机决定。下一步先对 `DIAN-029` 的模型／服务错误做可追踪的重复运行，核对调用阶段和错误类型；再完成测试集双人语义复核与金标冻结，并在独立物流配置上评估关闭向量分支。待这些证据稳定后设计真实 provider 的超时、鉴权、结果未知和幂等契约。当前分数只反映模拟工具世界的开发集流程，不是生产准确率或 SLA。

## 5. 复现方法

在仓库根目录、配置好本地 `.env` 后执行：

```powershell
python evaluation/datasets/dian_logistics_v1/validate_dataset.py
python -m pytest -q tests/test_logistics_cases.py tests/test_logistics_api.py tests/test_logistics_eval_runner.py
docker compose -f docker-compose.acceptance.yml -f docker-compose.local-check.yml build dian
```

每组依次运行，`<variant>` 取 `rules_only`、`llm_rules`、`full`；`<project>` 使用各不相同的 Compose 项目名，`<dir>` 使用各不相同的空目录。以下为 `llm_rules` 的实际命令形式：

```powershell
$env:DIAN_INTENT_MODE='llm_rules'
$env:DIAN_ACCEPTANCE_DATA_DIR='./data/ablation-replay/llm_rules'
docker compose -p dian-ablation-replay-llm -f docker-compose.acceptance.yml -f docker-compose.local-check.yml up -d --no-build --wait
python -m evaluation.logistics_eval_runner --split dev --variant llm_rules --base-url http://127.0.0.1:18000 --db data/ablation-replay/llm_rules/logistics/cases.sqlite3 --restart-command 'docker compose -p dian-ablation-replay-llm -f docker-compose.acceptance.yml -f docker-compose.local-check.yml restart dian' --output data/ablation-replay/llm_rules/eval/report.json
docker compose -p dian-ablation-replay-llm -f docker-compose.acceptance.yml -f docker-compose.local-check.yml down
```

runner 发现失败时会返回非零退出码并照常写报告；应查看 `summary` 和逐案 `failures`。`down` 不带 `-v`，保留独立验收卷。比较脚本用 `python -m evaluation.compare_logistics_ablation --root <三组报告的共同目录> --image-id <镜像ID> --output <比较报告路径>`；三组报告必须来自同一镜像、数据集与模拟规则版本。
