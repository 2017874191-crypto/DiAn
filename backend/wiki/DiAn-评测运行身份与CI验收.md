# DiAn 评测运行身份与 CI 验收

基线为 `ea97335`。旧比较器只校验分组、数据划分和案件集合。用三份相同案件、不同代码和策略版本的模拟报告实测，三份报告都被接受，比较器还给它们统一填入了调用者传入的镜像 ID。

## 评测契约

- 使用 `--variant`、`--provenance` 或 `--container` 时，回放器在发起业务请求前读取目标 API 的 `/eval/runtime`，校验实际意图模式、SQLite 工单后端，以及服务已加载的模拟数据和策略。独立验收 Compose 已开启该端点，其他部署通过 `DIAN_EVAL_PROVENANCE=true` 显式启用。启用鉴权时由管理员读取。
- 每次回放记录实际读取的数据集 SHA-256、选中的案件 ID、回放器 Git 提交（Git 可用时）和源码指纹。服务端记录启动时的源码与已安装 Python 依赖指纹，以及采集时的已加载规则、Skills、工具 Schema、模型标识、嵌入开关和列出的数值配置。记录使用结构化字段与摘要。
- Docker 回放可传 `--container`。回放器读取该容器的不可变本机镜像 ID，并核验目标主机名和回环地址发布端口。裸进程回放以源码和依赖指纹为证据，镜像字段留空。
- 在受控重启后和回放结束时重新取证。发现变化或无法读取时，报告照常保留已观察的结果，并写入核验失败原因。比较器拒绝缺失或核验失败的报告。三组可比报告只有实际 `intent_mode` 可不同，服务端或运行器身份、数据、镜像、环境、其他配置不同均报错。

## 验收

使用固定的三组开发集报告验证完整比较。首次模型或服务失败仍在分母中。分别混入不同的数据、代码、规则、模型、意图组名、镜像，或缺失和核验失败的报告，均应拒绝。另用真实 FastAPI 路由检查管理员权限、加载中策略变化、凭据隐去，以及实际运行中途漂移时的报告保留。

GitHub Actions 包含后端全量回归、独立真实 Redis 的陈旧写入实验、前端测试和生产构建。Redis 实验使用独立容器和两个进程，结果作为 CI 产物保留。这个流水线使用模拟模型或受控模型替身，付费模型回放和人工语义复核保持为独立任务。

本机 Windows / Python 3.11.9 后端全量 260 项通过。Node 24 下前端 17 项测试与构建通过。实际容器镜像检查成功，错误的目标主机名被拒绝。真实 Redis 7.4.12 的两个进程接管回归通过。

首轮云端后端回归失败。Linux 容器复现了 Temporal SDK 拒绝在 `fork` 子进程中复用父进程原生运行时的错误。Temporal 故障脚本和重启测试现在使用 `spawn` 启动独立 Worker，原有故障注入和断言照常运行。[首次 CI](https://github.com/2017874191-crypto/DiAn/actions/runs/44027783988)

在 `backend` 目录复现：

```powershell
python -m pytest -q tests/test_evaluation_provenance.py tests/test_logistics_eval_runner.py
python -m pytest -q tests
python -m evaluation.compare_stale_chat_write --output stale-chat-write.json
python -m evaluation.compare_logistics_ablation --root data/ablation-replay --output data/ablation-replay/comparison.json
```

三组正式回放使用独立验收栈和一套固定流程。每组的 `--variant` 应匹配实际部署，`--db` 指向该部署的 SQLite，Docker 运行时可追加 `--container <该组容器名或ID>`。历史报告继续供查阅，只有重新运行才能获得新格式的取证记录。

## 适用范围

身份校验依赖受控目标和取证时点。源码指纹用于发现内容变化，容器 ID 用于本机镜像识别。外部模型快照、Redis 或 Chroma 的内容与冷热缓存、外部服务健康以及两次采样之间的临时变化，需要由独立部署规程或专项测试约束。自动流程断言和 CI 结果均按模拟场景解释。
