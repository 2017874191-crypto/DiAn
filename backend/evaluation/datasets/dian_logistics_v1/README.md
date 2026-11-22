# DiAn 物流异常案件评测集 v1

首版共 50 个中文案件、73 个用户轮次：40 个基于实际开源记录改写，10 个根据项目工程约束原创构造。开发集 30 个，测试集 20 个，按 24 个场景族划分。以下保留首版的数据制作与冻结记录；后续运行状态和判分版本见文末补记，不能把首版“未运行”当作当前状态。

## 来源筛选

| 来源 | 已读取数据 | 本版采用 | 用途与限制 |
|---|---:|---:|---|
| Bitext Retail/eCommerce | 44,884 行 | 24 条 | 运输破损、错发、丢失、普通查件、时效与运费；原始数据是英语训练集 |
| ABCD v1.1 | 10,042 段对话 | 12 条 | 多轮未收货、发运核验、寄送修改、错色及运费争议；本版取其 dev 记录 |
| Bitext Customer Support | 26,872 行 | 4 条 | 人工联系和退款进度的路由边界 |
| DiAn 自建工程场景 | 不适用 | 10 条 | 缺字段、归属、冲突、超时、幂等、重启、确认、转人工与提示注入 |

官方来源：
- https://huggingface.co/datasets/bitext/Bitext-retail-ecommerce-llm-chatbot-training-dataset
- https://github.com/asappresearch/abcd
- https://github.com/bitext/customer-support-llm-chatbot-training-dataset

Bitext 两份数据的上游许可标记为 CDLA-Sharing-1.0，ABCD 仓库为 MIT。LICENSES 内保留官方许可文本；每条来源记录保留自己的许可标识，不把整包错误标成 MIT。新增工程用例未另行指定公开再分发许可。本包没有重新分发全部原始语料。

## 如何改写

保留用户问题语义，中文重新表述；移除英语种子中的辱骂，不复制虚构个人姓名、电话、地址或账号。订单和身份替换成项目的 DEMO1001–DEMO1004 与 demo-user。source_records.jsonl 记录原始行号、对话 ID、原意图、原 split、内容哈希和定位链接。

签收争议的两条 Bitext 种子原本只表达“未收到”，本版增加已送达的工具世界，明确标为场景增强，不声称上游有签收争议标签。工程异常的十条完全标为 project_engineering_synthetic，不伪装成开源基准原题。

原数据中的助手答复与业务动作没有直接充当本项目标准答案。状态、权限、确认要求和工单数量依据固定的 DiAn 演示规则补充。源场景也不等于承运商真实政策。

## 文件

- all.jsonl：全部 50 个完整案件；dev.jsonl / test.jsonl：互斥划分。
- 用例清单.md：便于逐条审核的中文表格。
- schema.json：JSON Schema。
- sources.json / source_records.jsonl：原始文件版本、哈希、许可与记录定位。
- fixtures.json / policies.json：本版冻结的模拟工具世界与规则。
- validate_dataset.py：离线结构、分组、重复项与来源检查。

## 使用与判分

每个案件使用新的 conv_id，setup.user_id 为身份。依次发送 turns[].message。比较每轮的 /chat.logistics_case 与 GET /logistics/case 快照；agent_any_of 是允许的角色集合，不能解读为多 Agent 协作列表。required_tools 是必须发生的工具，forbidden_tools 是不得发生的工具。ticket_count 为当前测试会话在 SQLite 的工单总数，不是整库工单数。

case_state=null 表示不应自动创建异常案件，主要用于普通咨询、费用、退款、改址和人工联系等边界。case_issue 是 DiAn 目标业务标签，不是上游原始 intent。semantic_checks 需要后续人工或经过校准的 Judge 判定，不能只做字符串匹配；程序状态和副作用数量优先用确定性检查。

replay_of_turn 为零起始轮次，必须复用该轮 client_request_id 和消息，验证原回执。restart_services 需要隔离测试环境和受控重启能力，普通 HTTP runner 应标记“不支持/未执行”，不能忽略后算作通过。普通咨询在部分现有路径中可能给出引导而不查物流，本版不会强制每条普通问答调用查询工具。

本文件格式是案件级评测契约，不直接接入原有 /eval/run，也不能作为 IntentTestCase 列表导入。已有独立 runner：`evaluation.logistics_eval_runner`，检查实际 API、工具轨迹与数据库副作用。

## 验证命令

```powershell
python evaluation/datasets/dian_logistics_v1/validate_dataset.py
# 如仍保留本次下载缓存，可以额外逐条核验来源：
python evaluation/datasets/dian_logistics_v1/validate_dataset.py --source-cache <缓存目录>
```

## 评测约束

数据按场景族分组，避免同一原记录或同一族跨开发集与测试集；使用了开源训练语料与 ABCD dev 子集，因此这是“基于开源语料改写的项目评测集”，不是上游官方 benchmark 分数。各 split 类别并非严格均衡，应按来源、类型、边界和工程用例分别报告。

测试集先留作后续冻结评测；如果据此调提示词或规则，应把已暴露用例降为回归集，并重新建设未参与调参的测试集。数据较小，模板和 DEMO 编号仍有偏差；不能据此推断生产准确率或未见承运商的泛化能力。

固定工具世界使用静态时间戳，本版不测真实运输时效、法律赔付规则或在线承运商 SLA。期望动作按业务约束设置，没有调用当前实现把其输出反写为金标；部分用例可能暴露当前路由或状态处理缺陷。

## 当前验证状态

首版制作时完成50条结构与分组检查、全部40个上游记录匹配和原文件哈希核验。后续已做模型开发回归和物流消融，详见项目验收记录；独立人工双人标注与一致性分析尚未完成。当前公开题包及已参与开发的题目均按开发回归处理，文件名`test`不代表仍有盲测资格。

## 2026-11-22 判分契约版本

JSONL、原金标与历史报告保持原样。runner新增显式`--contract`，默认`legacy-v1`以保留历史口径；当前破损人工审核使用`manual-review-v2`。两个版本的分数不能直接混用。

| 约束 | legacy-v1 | manual-review-v2 |
| --- | --- | --- |
| 破损提交依赖物流读取 | 按旧required_tools，并要求固定三步核验顺序 | 不再要求query_logistics；仍需归属与政策核验先于建单，二者之间不限顺序 |
| 具名材料完整性 | 历史状态、工具和数量断言 | 待确认/已建单须声明外包装、面单、商品问题三类材料；建单前一轮也须完整 |
| 丢件、签收争议的物流读取 | 必须 | 仍必须 |
| 身份、事项确认、工单数量、禁止工具 | 必须 | 仍必须；确认须与问题类型相符 |
| 自然语言语义 | 待人工复核 | 仍待人工复核，不以流程通过替代 |

v2只显式改变上述路径契约，不根据候选输出重写题目的期望状态、问题类型或工单数量；旧题与现有咨询边界如有冲突，仍报告失败并另行复核。材料常量独立保存在评分器，不从候选案件的required_evidence反推标准。两类runner的报告均写入`evaluation_contract`，首次失败不得覆盖。

仅在隔离、可丢弃的模拟部署上执行，例如从backend目录运行：

```powershell
python -m evaluation.logistics_eval_runner --contract manual-review-v2 --split dev --case-id DIAN-001 --base-url http://127.0.0.1:18008 --db data/acceptance/materials-20261122/cases.sqlite3 --provenance --variant full --output data/acceptance/materials-20261122/current-contract.json
```

带鉴权的部署另传用户及管理员测试凭据，不把凭据写入公开命令记录。此命令只选1案验证新契约能运行，不代表50案整体通过，更不提供独立泛化结论。
