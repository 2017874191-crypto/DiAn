# DiAn Agent Infra 面试演示与追问

当前清单对应演示候选`interview-2026-11-23-rc1`，业务检查基线为`1f1d04f`，涵盖旧快照后的材料完整性、备份恢复及版本迁移修复。标签仅在对应提交CI通过后建立，见[冻结范围](DiAn-20261123候选版冻结说明.md)。原`interview-2026-11-22`（`052620f`）不移动，不能当作包含这些修复的版本；后续main变更也不自动算入本候选。技术来源见[NOTICE](../../NOTICE.md)，贡献见[简历包装](简历包装.md)，状态及默认值见[证据盘点](DiAn-AgentInfra工程能力审查与优化方案.md)。

## 统一验收清单与运行边界

| 演示／检查 | 当前可执行入口 | 预期结果 | 依赖与证据边界 |
| --- | --- | --- | --- |
| 材料、确认、归属、重放 | 下方审核API测试 | 材料不全不能确认；齐全后独立确认建1张；拒绝/非本人不建单，重复返回原结果 | 实际ASGI及SQLite，模型与Redis替身；不是模型质量评分 |
| 远端未知结果恢复 | 下方2项HTTP故障测试 | 本地unknown/prepared最终查回原单，远端总数1 | 独立本地HTTP模拟服务和数据库、受控进程；不接真实工单系统 |
| 稳定键必要性 | 下方稳定键消融 | 相同键1张，错误换键2张 | 反例仅在隔离模拟库中执行 |
| 备份恢复 | tests/test_logistics_backup.py | WAL主文件复制对照漏数据；SQLite快照完整；恢复重复确认及未知结果核对不增单 | 临时库、独立HTTP模拟服务；不恢复Redis，不承诺备份后操作自动找回 |
| 版本迁移 | tests/test_logistics_schema.py | 0→1保留业务身份，同版本跳过，失败全回滚，高版本拒绝 | 隔离库；不是生产在线升级或任意版本降级 |
| 快递鸟适配器 | tests/test_kdniao_tracking.py | 授权/协议/排序/失败分类及两类历史来源正确 | MockTransport，不访问快递鸟或模型 |
| 已保存的真实查询 | kdniao_trial_closeout_20261123.json | 实际授权请求1次、16节点；重放及越权新增0次 | 2026-11-23历史证据，本地绑定、fakeredis及临时SQLite；不是现场实时查询 |
| 历史响应模型回放（可选） | 下方显式response-fixture命令 | captured_kdniao_response、query_performed=false、queried_at=null | 快递鸟0次；模型仍联网并可能计费，未作为本轮必跑项 |
| 前端检查 | npm test及npm run build | 请求状态与结构化案件检查通过、构建成功 | 不等于浏览器验收；页面历史证据见主链路文档 |

现有后端开发依赖包含pytest、fakeredis、Temporal SDK等；以下选定检查不会启动Temporal服务，也不需要Docker、模型凭据或物流额度。完整依赖安装按根README，目标CI环境为Linux/Python3.12；本机既有环境为Windows/Python3.11.9，两者结果分别记录。不复制覆盖现有.env。

**统一工程检查**：从backend目录运行以下一条命令，使用临时SQLite和自有模拟进程；需要允许本机回环HTTP及子进程。

```powershell
python -m pytest -q tests/test_chat_reliability.py::test_damage_chat_reaches_ticket_and_preserves_refusal_ownership_and_replay tests/test_logistics_http_operation.py::test_response_lost_reconciles_without_duplicate tests/test_logistics_http_operation.py::test_process_crash_after_remote_commit_recovers tests/test_logistics_http_operation.py::test_ablation_unstable_key_creates_second_remote_ticket tests/test_logistics_backup.py tests/test_logistics_schema.py tests/test_kdniao_tracking.py
```

从frontend目录运行：

```powershell
npm test
npm run build
```

2026-11-23对业务基线1f1d04f执行：后端上述17项通过，前端17项通过，构建成功，无模型/快递鸟调用，当前业务库未改动。[本轮汇总](../evaluation/candidate_checklist_acceptance_20261123.json)记录源码摘要、环境、用例名及原始JUnit哈希。17项是选定工程断言的数量，不是17个真实用户任务，后端与前端也不能相加成准确率。本轮没有新增测试或完整部署／浏览器复验。

历史scripts/accept_logistics_p0.py是P0阶段入口，测试范围及legacy默认不等于本清单。50案开发回放若另行执行，应使用独立runner并明确manual-review-v2；本轮未重跑50案，也没有更改金标。

## 60秒开场

> 我设计并实现了DiAn的物流领域工作流与可靠执行机制，重点解决的是：模型理解用户表达以后，怎样可靠地执行一个带确认的业务动作。
>
> 我把订单归属、材料声明、确认和工单状态交给领域代码，增加了持久化执行记录、稳定操作键和结果核对，处理“远端已成功，本地没收到结果”的窗口。同时限制并发、排队和模型调用预算。
>
> 项目已接入一条真实物流只读接口；工单、赔付和图片审核仍明确受限。面试主要展示完整审核、故障恢复和真实查询证据，以及为什么一些复杂机制没有默认开启。

## 八分钟演示顺序

| 时间 | 展示 | 需要让面试官看到的事实 |
| --- | --- | --- |
| 0:00—1:00 | 开场、目标与结构 | 项目解决什么问题，个人设计与实现覆盖哪些部分；这是有边界的领域执行服务 |
| 1:00—3:00 | 完整模拟审核 | 材料是声明，提交前独立确认；重复仍为原工单，拒绝及非本人订单不能写入 |
| 3:00—5:00 | 丢响应和进程退出 | 本地未知不代表远端失败；按原键取回结果，远端工单仍1张 |
| 5:00—6:00 | 已保存的真实查询证据 | 真实供应商返回16条节点，外部调用1次，重放与越权请求不出站 |
| 6:00—7:00 | 备份和迁移证据 | 快照包含WAL，恢复保留操作键，升级失败整体回滚；仍有恢复窗口限制 |
| 7:00—8:00 | 一个消融和两个取舍 | 去掉稳定键会多一张单；Temporal可选，三路融合未被包装成已证明收益 |

### 1. 审核链路

从`backend`目录运行，沿用已经安装的依赖：

```powershell
python -m pytest -q tests/test_chat_reliability.py -k damage_chat_reaches_ticket
```

同时打开[API回归代码](../tests/test_chat_reliability.py)，定位`test_damage_chat_reaches_ticket_and_preserves_refusal_ownership_and_replay`。说明模型识别与Redis使用替身以固定输入，实际`/chat`、领域状态和SQLite执行；这不是一次真实模型质量评分。演示关注API返回与数据库最终工单数，而不是只展示绿色测试数量。

若使用已有前端和模型配置现场对话，选择`demo-user`、`DEMO1003`，依次发送：

1. “订单 DEMO1003 商品破损，请申请人工审核。”
2. “外包装照片已经提供。”
3. “我同意提交工单。”
4. “面单照片已经上传。”
5. “商品问题照片已经提供。”
6. “我同意提交工单。”
7. “同一件事情再提交一次。”

预期：第2、3、4步均仍待材料且工单0，第5步才待确认，第6步建1张，第7步返回原工单。请使用新的演示会话及没有未结同类工单的模拟订单；若已存在未结同类案件，应展示去重结果，不为现场演示清空共享库。拒绝应在另一笔订单的待确认阶段验证，非本人使用独立身份；不能在已建单案件里再拒绝，并把原有工单误记为拒绝后新建。页面曾在2026-11-22验证过，最新候选部署仍需现场预演，不能将历史页面记录当成本轮浏览器验收。确定性API与真实模型记录分别见[主链路文档](DiAn-破损主链路与咨询收敛验收.md)。

### 2. 故障恢复

```powershell
python -m pytest -q tests/test_logistics_http_operation.py -k "response_lost_reconciles_without_duplicate or process_crash_after_remote_commit_recovers"
```

打开[对应测试](../tests/test_logistics_http_operation.py)和[Operation领域实现](../core/logistics.py)。指出独立HTTP服务和另一份数据库是真实启动的模拟业务系统；替换的是故障时序，不是结果核对器。

- 丢响应：远端已有1张，本地记录`unknown`；重建本地服务后，按原键查询得到原工单。
- 进程退出：远端提交后调用进程以指定退出码结束，本地仍为`prepared`；新服务先查原键，不直接重建。
- 保证依赖远端同键去重、参数一致和权威结果查询。不能把这个演示概括为跨系统事务或生产exactly-once。

### 3. 稳定键消融

```powershell
python -m pytest -q tests/test_logistics_http_operation.py -k ablation_unstable_key_creates_second_remote_ticket
```

展示同键重复创建仍1张，重试改成新键后变2张。它支持“稳定键配合远端唯一约束必要”，没有单独证明Operation记录、先查再写和幂等键各自的全部贡献。

### 4. 真实只读证据

打开[公开脱敏汇总](../evaluation/kdniao_live_8002_acceptance_20261122.json)：核对`source`、查询时间、最新节点时间、城市和三个场景的外部调用计数。明确它来自2026-11-22的真实请求，不是现场新请求；回执中的查询时间对应原请求，不能冒充实时刷新。

更新的[试用收尾汇总](../evaluation/kdniao_trial_closeout_20261123.json)包含2026-11-23一次真实读取及实际响应脱敏回放，两轮供应商调用分别为1和0。它与11月22日记录是同一票授权运单的不同查询，不算两票或不同承运商覆盖。不需要为演示再消耗查询额度。

可选模型联调，从backend目录运行：

```powershell
python -m evaluation.verify_kdniao_chat --response-fixture evaluation/fixtures/kdniao_live_captured_redacted_20261123.json --output data/kdniao/interview-captured-replay.json
```

这条命令不访问快递鸟，但会调用已配置模型；来源明确为项目保存的脱敏历史响应，缺少城市和原始描述是主动脱敏，不让模型补造。完全无模型的适配器演示使用上表pytest入口。只有现场确需实时查询且供应商权限、模型和授权绑定均有效时，才使用[真实查询命令](DiAn-快递鸟8002只读接入验收.md)；本清单未执行它。

### 5. 备份恢复与版本迁移

```powershell
python -m pytest -q tests/test_logistics_backup.py tests/test_logistics_schema.py
```

查看[备份结果](../evaluation/sqlite_backup_restore_acceptance_20261123.json)和[迁移结果](../evaluation/sqlite_schema_acceptance_20261123.json)：重点是主文件复制工单0与正确快照工单1的对照、恢复后沿用原键查询，以及DDL/数据/版本号原子回滚。操作命令和恢复开放条件见[运维说明](DiAn-AgentInfra工程能力审查与优化方案.md)。不要在面试时对当前业务库直接执行恢复或迁移，也不要手动设置版本号冒充升级。

## 追问与回答边界

| 追问 | 可讲清楚的回答 |
| --- | --- |
| 为什么不用纯状态机，为什么又不让Agent决定一切？ | LLM处理自然表达、知识问答和角色选择；授权、确认、状态变化和副作用必须有确定性边界。不是为了增加Agent数量。 |
| 请求ID、案件ID、Operation键有什么区别？ | 请求ID处理传输重放；案件记录聚合业务进展，活动案件按已核验用户/订单/问题去重；Operation键固定一次副作用和参数。同一个请求重放与新请求主动核对不能混用。 |
| 你实现了exactly-once吗？ | 没有跨系统事务。模拟合同下同一稳定键最多一张远端工单，未知结果先查询。若真实下游不支持幂等或查询最终一致，不能把404当确定未执行，需保留未知并人工核对。 |
| 本地数据库写入后、外部调用前崩溃怎么办？ | 操作意图先持久化，恢复时沿用原键查询；根据已保存状态和权威查询结果继续。已经观察到远端成功但本地写失败，也不能生成新键盲重试。 |
| 超时或取消是不是撤销了业务？ | 不是。已开始的线程调用可能继续，执行结束前继续占用相应额度；客户端查询原结果。未知态和已失败必须区分。 |
| 为什么Temporal不默认开？ | 已做最小接入和故障/成本对照。它提供持久调度，却不能替代业务幂等和结果核对。当前短流程没有充分净收益证据，保留可选；长时间等待、事件驱动任务成为需求后再评估。 |
| 三路识别是不是三名独立专家投票？ | 不是。LLM自报分、相似度和规则分不在同一概率尺度，也彼此相关。固定加权是工程启发式；历史差异含服务错误，不能归因为向量收益。 |
| 你把RAG改成BM25了吗？ | BM25用于部分开发评测适配，不能说生产检索已经整体替换。知识库工具仍由模型选择是否调用，意图分类不是检索权限硬门控；没有确定性无幻觉保证。 |
| 备份恢复和迁移是不是生产灾备？ | 不是。快照包含已提交WAL和持久表；未知操作能恢复的前提是原键已在快照里。Redis回执及备份后新操作另需核对；本轮仅0→1迁移和上一版领域模块兼容，不承诺异地容灾、零RPO或任意版本回退。 |
| 怎样防止把测试做成针对题目的补丁？ | 先固定成功定义和候选，保留首次结果；已经看过的题只算开发回归。代码断言核对最终工单，Judge只辅助，独立人工复核仍为0，不合成漂亮的总体准确率。 |
| 这个项目距生产还有什么？ | 真正的订单身份与租户授权、真实写接口契约、文件证据审核、目标部署下的容量和可用性验证。现有额度是单进程约束，不是分布式全局配额；SQLite演示不等于多节点高可用。 |

上述回答只对应仓库已有能力。时间、吞吐、准确率和生产经历没有证据就不补；面试应能打开代码解释控制条件，而不只背答案。

## 演示前准备

- 当前清单面向RC1源码，旧冻结标签不移动；演示前记录git rev-parse HEAD和所用配置，核对当前检出是否为该标签。按README确认依赖，不覆盖现有.env，也不投屏包含凭据的配置或进程环境。
- 先运行上面选定的工程演示；它们使用临时数据库及自有模拟进程，不依赖真实快递鸟、模型或Docker。
- 准备脱敏查询汇总、领域状态机代码、HTTP工单测试和一个取舍报告。原始电话、运单和密钥不投屏。
- 网络不稳定时展示已有记录并说明来源；模型调用失败不能伪装为成功回复。
- 不用历史50案通过率代表当前版本：旧固定工具路径与新破损契约有差异，详细限制见[快照说明](DiAn-20261122演示快照说明.md)。
- 明确区分“源码默认”“本地.env”“本次演示实际配置”；当前默认full及物流向量开启、Temporal关闭，未因某次隔离实验自动改变。三路和RAG、多角色的收益边界见当前证据盘点。

补充证据：[有界执行](DiAn-有界执行与模型预算验收.md)、[Temporal成本与边界](DiAn-真实Redis断线与资源对照验收.md)、[HTTP Operation](DiAn-HTTP工单Operation验收.md)、[主链路及咨询收敛](DiAn-破损主链路与咨询收敛验收.md)。

历史复现：2026-11-22曾执行当时选定命令，API审核1项、HTTP故障及稳定键消融3项通过。2026-11-23当前统一检查结果见顶部；两次分母重叠，不合并成业务成功率。没有独立人工语义复核，不能凭这些工程检查宣布M4全部完成。
