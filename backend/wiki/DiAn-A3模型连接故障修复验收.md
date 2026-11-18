# DiAn A3 模型连接故障修复验收

## 原因与复现

继承的 `ANTHROPIC_BASE_URL` 指向旧模型服务地址，项目 `.env` 配置了 DeepSeek 官方地址。旧加载方式优先使用环境变量。排查时旧域名解析返回 Windows `gaierror 11001`，这是当前可确认的配置与解析故障。

显式选择项目配置后，HTTPX 仍可能读取 Windows 系统代理。安装版本 HTTPX 0.28.1 的 `get_environment_proxies` 调用 `urllib.request.getproxies()`，后者会读系统注册表。实测 TCP 对端为本机代理，两个失败的 trace 均为 `proxy.start_tls.failed`，在约 5 秒后以 `APIConnectionError → ConnectError → ConnectError` 返回。

在相同模型和本地配置下，共享一个代理连接连续请求 6 次全部成功，随后逐次新建连接的 6 次请求有 2 次失败。关闭环境代理读取、逐次新建直连连接的 8 次请求全部成功。这定位了本轮复现故障的代理 TLS 阶段。此前十次历史错误只记录外层异常，逐笔底层原因无法从旧日志还原。

本机对解析出的 15 个官方 CDN 地址另做校验证书的 TLS 探测，13 个成功、2 个在 7 秒内未完成，说明直连路径也需要持续观测。全部探测使用默认 TLS 证书校验。以上采样按顺序执行，时间与节点可能影响结果，连接复用的长期收益仍需更大样本。

## 修改

- RAG 评测按显式配置文件、项目 `.env`、环境变量选择一整组地址、密钥和模型。所选文件缺少密钥时明确报错。报告仅记录来源类型及被覆盖的变量名。
- 评测默认 `--network direct`，HTTPX `trust_env=False`。需要系统代理时指定 `--network system`。设置作用于评测模型客户端。
- 同一次评测共享一个 SDK/HTTP 客户端，各组通过现有 `fork()` 隔离工具统计、缓存和熔断。SDK 重试次数为 0，连接超时 5 秒，读取超时 30 秒，连接池上限 4，空闲连接保留 30 秒。
- 记录 TCP 建连、直连 TLS 与代理 TLS 的连接或失败计数。诊断字段避开原始异常正文、请求头和密钥。客户端在评测结束时统一关闭。

客户端可通过 `MCPToolManager(..., client=...)` 注入，由调用方管理生命周期。常规构造沿用原有的独立客户端行为。原理说明见 [HTTPX 环境设置](https://www.python-httpx.org/environment_variables/)与[连接复用](https://www.python-httpx.org/advanced/clients/)。

## 验证

在 `backend` 目录使用原先固定的公开子集运行。默认项目配置与直连方式无需额外参数。

```powershell
python -m pytest -q tests/test_rag_evaluation.py tests/test_tool_manager.py tests/test_knowledge_base.py tests/test_llm_utils.py
python -m evaluation.compare_rag --rounds 3 --limit 8 --output data/rag-a3/connection-fixed.json
```

专项 33 项测试通过，覆盖配置整组选择、文件缺失与缺密钥、共享客户端生命周期、各组工具状态隔离和脱敏网络诊断。原有失败及部分回退结果继续保留在 [A3 初次运行](DiAn-A3检索评测最小接入验收.md)。
