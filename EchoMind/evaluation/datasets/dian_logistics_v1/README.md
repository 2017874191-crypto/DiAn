# DiAn 物流异常案件评测集 v1

首版共 50 个中文案件、73 个用户轮次：40 个基于实际开源记录改写，10 个根据项目工程约束原创构造。开发集 30 个，测试集 20 个，按 24 个场景族划分。当前完成来源核验与结构验证，尚未运行模型评分，金标需要人工复核。

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

本文件格式是新建的案件级评测契约，目前没有自动接入现有 /eval/run。不要把它直接当成原有 IntentTestCase 列表导入；下一步需要实现读取、工具钩子、分项计分和报告的案件级 runner。

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

完成 50 条结构与分组检查、全部 40 个上游记录匹配和原文件哈希核验。模型评测、性能统计、人工双人标注及一致性分析均未执行。
