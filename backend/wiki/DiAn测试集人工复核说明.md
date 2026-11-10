# DiAn 冻结测试集人工复核说明

## 范围与文件

冻结测试集 `dian_logistics_v1/test.jsonl` 有 20 个案件、28 个用户轮次。复核表由 `evaluation/prepare_logistics_review.py` 生成，当前副本位于 `data/acceptance/eval/test-review.csv`；该目录不会提交到 Git。每一行是一轮用户输入，不是一个独立案件。表中预留两位复核者、裁决者和最终结论字段，当前全部为空。

生成空白表：

```powershell
python -m evaluation.prepare_logistics_review --split test --output data/acceptance/eval/test-review.csv
```

供两位复核者独立填写的工作表已分别生成，均为 20 案、28 轮，且只包含各自的姓名、结论和理由栏：

```powershell
python -m evaluation.prepare_logistics_review --split test --reviewer-slot 1 --output data/acceptance/eval/test-reviewer-1-blank.csv
python -m evaluation.prepare_logistics_review --split test --reviewer-slot 2 --output data/acceptance/eval/test-reviewer-2-blank.csv
```

两份空白表应分别交给不同复核者，收回前不要互相共享。当前尚无任何复核者判定；不能用脚本或模型代填姓名、结论和理由。

收回后，先校验两份表与冻结测试集逐行一致、复核者身份不同、修订或不确定结论有理由，再统计一致与分歧：

```powershell
python -m evaluation.validate_logistics_review --first <复核者1已填写表.csv> --second <复核者2已填写表.csv>
```

`ready_for_adjudication=true` 只表示两份表均填完且结构有效，分歧仍须由第三人裁决；脚本始终报告 `gold_frozen=false`，不会自动改写测试集或宣布金标已冻结。两份当前空白表校验结果为 0/28、0/28，`ready_for_adjudication=false`。

真实回放完成后，如需把实际回复写入新表，使用 `--report <测试集回放报告.json>`，并指定新的 `--output` 路径。脚本拒绝覆盖已有文件，避免抹去复核意见。报告必须与所选 split 一致。`observed_response` 为空时，只能复核金标与工具世界，不得给模型语义表现打分。

## 独立复核规则

1. 两位复核者分别查看用户输入、前序轮次、固定模拟订单/物流事实、业务规则、来源和期望字段，不互看彼此判定。
2. 先判断期望状态、案件类型、允许角色、工单数量、必需/禁止工具是否与项目规则一致，再判断每条 `semantic_checks` 是否合理。
3. 若表中有实际回复，逐条核对是否把模拟查询说成真实查询、是否宣称联系承运商或完成赔付、是否把用户陈述当作责任结论、是否清楚说明下一步。
4. `reviewer_1_verdict` / `reviewer_2_verdict` 仅填 `accept`、`revise` 或 `uncertain`；后两者必须写具体原因和建议金标。每位复核者填写自己的姓名或可追溯代号。
5. 两人不一致时由第三人裁决，在 `final_verdict` 填 `accept`、`revise` 或 `exclude`，写明依据。不得以当前实现输出反向修改金标。
6. 汇总时按案件和场景族报告一致率、分歧数、修订数、剔除数；多轮案件需整案检查状态衔接。复核前不要宣称测试集分数是可信业务准确率。

## 冻结与版本

- 原始 `test.jsonl` 保持冻结。复核意见和裁决保存在独立文件；金标修订形成新版本并重新校验哈希、来源与场景族划分。
- 如果使用测试集结果调整路由、提示词或规则，已暴露案件应转为回归集，另建未参与调参的测试集。
- 评测报告必须记录代码版本、数据集版本、模型 ID、工具世界哈希、运行时间和未执行用例。模型调用失败、环境失败与业务流程失败分开统计。

当前状态：仅生成复核表，尚无第一、第二复核者签名，也未运行冻结测试集的模型回放。
