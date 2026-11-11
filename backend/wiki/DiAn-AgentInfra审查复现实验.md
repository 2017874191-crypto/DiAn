# DiAn Agent Infra 审查复现实验

日期：2026-11-11

审查基线：59bca6bb258551885ff5248a046ba78e88db4c5f

关联文档：[工程能力审查与优化方案](DiAn-AgentInfra工程能力审查与优化方案.md)

## 1. 环境、范围与结果

本轮使用 Windows 宿主 Python 3.11.9；项目声明 Python >=3.12，Docker 基础镜像为 Python 3.12。以下实验补充静态审查，不替代目标版本的部署验收。所需依赖已安装，没有调用真实模型、承运商或工单服务。

相关测试运行结果：85 passed，1 warning in 48.72s。另有 pytest-asyncio 对未设置默认 fixture loop scope 的配置提醒。测试中存在依赖模拟，不能把这些单测结果解释为真实 Redis、跨进程、跨主机的故障验收。

在 DiAn/backend 目录执行：

~~~powershell
python -m pytest -q -p no:cacheprovider tests/test_logistics_cases.py tests/test_logistics_api.py tests/test_logistics_eval_runner.py tests/test_chat_reliability.py tests/test_concurrency.py tests/test_tool_manager.py tests/test_standard_mcp.py tests/test_eval_jobs.py tests/test_performance.py
~~~

| 探针 | 本轮有效观察 |
| --- | --- |
| T1 | injected_delay_ms=120，deadline_ms=20，elapsed_ms=125.0，timeout_raised=false，heartbeat_ms=125.0 |
| T2 | state 原 TTL=86399 秒（刚写入约 86400）；受控过期后 state={}，SQLite case_persisted=true |
| T3 | 同订单、同用户、不同 conv_id，distinct_cases=2，tickets=2 |
| T4 | 构造错误先后顺序及失败核验结果，evaluate_turn 返回 failures=[] |

上述探针分批执行，以下合并脚本包含相同调用路径与输入，便于后续复现；时延随机器变化，不应断言恰好 125ms。T2 使用 FakeRedis 的毫秒过期并等待一个调度间隔，不把模拟器边界当成 Redis 真实服务故障测试。

文档完成后直接提取下方代码再次执行：T1 elapsed_ms/heartbeat_ms 均为 141.0、timeout_raised=false；T2–T4 与上表一致。此为文档代码的复现校验，不是额外的压力测试。

## 2. 无真实外部调用的复现代码

代码仅在系统临时目录创建 SQLite，并在退出时清理；使用单独 FakeRedis 内存实例。它不创建完整 AgentOrchestrator，也不初始化真实 Redis/Chroma 连接。LogisticsAgent 使用真实 _call_llm 方法，订单 Provider 仅在 T1 包装人为延迟，其他案件逻辑和 SQLite 工单代码保持原样。

在 DiAn/backend 目录，以已经安装项目依赖的 Python 执行以下代码。可以保存为临时脚本执行，或在 PowerShell 使用 here-string 管道传给 python -B -。本文件为审查材料，没有自动把探针加入产品测试。

~~~python
import asyncio
import json
import tempfile
import time
from pathlib import Path

import fakeredis.aioredis
from agents.agent_orchestrator import LogisticsAgent, Request
from core.intent_recognizer import IntentCategory
from core.logistics import LogisticsService
from evaluation.logistics_eval_runner import evaluate_turn
from memory.conversation_memory import MemoryManager


def request(conv_id, message="包裹疑似丢件", state=None):
    return Request(
        message=message,
        user_id="demo-user",
        conv_id=conv_id,
        intent=IntentCategory.LOGISTICS_LOST,
        intent_group="logistics",
        entities={"order_id": ["DEMO1001"]},
        conversation_state=state or {},
    )


async def main():
    with tempfile.TemporaryDirectory(prefix="dian-infra-audit-") as tmp:
        service = LogisticsService(Path(tmp) / "cases.sqlite3")

        # T1：调用真实物流执行路径，仅给订单 Provider 注入阻塞。
        original_order = service.order_provider

        class SlowOrder:
            def query_order(self, *args):
                time.sleep(0.12)
                return original_order.query_order(*args)

        service.order_provider = SlowOrder()
        agent = object.__new__(LogisticsAgent)
        agent.service = service
        started = time.monotonic()

        async def heartbeat():
            await asyncio.sleep(0.01)
            return round((time.monotonic() - started) * 1000, 1)

        pulse = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        timed_out = False
        try:
            async with asyncio.timeout(0.02):
                await agent._call_llm(request("blocking"))
        except TimeoutError:
            timed_out = True
        elapsed = round((time.monotonic() - started) * 1000, 1)
        print(json.dumps({
            "probe": "T1_sync_provider_blocks_event_loop",
            "injected_delay_ms": 120,
            "deadline_ms": 20,
            "elapsed_ms": elapsed,
            "timeout_raised": timed_out,
            "heartbeat_ms": await pulse,
        }))
        service.order_provider = original_order

        # T2：仅让派生会话状态过期，保留持久案件。
        first = request("expiry")
        result = service.process(first)
        memory = object.__new__(MemoryManager)
        memory._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await memory.commit_turn(
            "demo-user", "expiry", "expiry-request", first.message,
            {"status": "success", "response": "ok"},
            state=first.conversation_state,
        )
        key = memory._state_key("demo-user", "expiry")
        configured_ttl = await memory._redis.ttl(key)
        await memory._redis.pexpire(key, 1)
        await asyncio.sleep(0.03)
        print(json.dumps({
            "probe": "T2_state_pointer_expiry",
            "ttl_s_before": configured_ttl,
            "state_after": await memory.get_conversation_state(
                "demo-user", "expiry"
            ),
            "case_persisted": bool(service.get_case(
                "demo-user", result["case"]["case_id"], "expiry"
            )),
        }))
        await memory._redis.aclose()

        # T3：同一业务对象通过不同会话申请，两次都明确确认。
        case_ids = []
        for conv_id in ("duplicate-a", "duplicate-b"):
            first = request(conv_id)
            service.process(first)
            result = service.process(request(
                conv_id, "确认建单", first.conversation_state
            ))
            case_ids.append(result["case"]["case_id"])
        with service.connect() as db:
            tickets = db.execute("SELECT count(*) FROM tickets").fetchone()[0]
        print(json.dumps({
            "probe": "T3_same_order_across_conversations",
            "distinct_cases": len(set(case_ids)),
            "tickets": tickets,
        }))

    # T4：测试断言器，而非伪造真实服务的执行结果。
    # 必要工具都出现、状态与计数相同，但先建单后核验且核验失败。
    view = {"state": "ticket_created", "issue": "logistics_lost"}
    expected = {
        "case_state": "ticket_created",
        "case_issue": "logistics_lost",
        "agent_any_of": ["logistics"],
        "ticket_count": 1,
        "required_tools": ["query_order", "create_logistics_case"],
        "forbidden_tools": [],
    }
    response = {
        "status": "success",
        "logistics_case": view,
        "agent_type": "logistics",
        "request_id": "trace-probe",
    }
    trace = {
        "found": True,
        "trace": {
            "request_id": "trace-probe",
            "tool_calls": [
                {"tool_name": "create_logistics_case",
                 "success": True, "result_success": True},
                {"tool_name": "query_order",
                 "success": True, "result_success": False},
            ],
        },
    }
    print(json.dumps({
        "probe": "T4_trace_order_and_result_not_asserted",
        "failures": evaluate_turn(expected, response, view, trace, 1),
    }))


asyncio.run(main())
~~~

## 3. 如何解释与转换为后续验收

T1 是执行边界实验，证明同步函数占用事件循环；不模拟真实网络成功/失败，也不提供 QPS 结论。后续应对独立 HTTP Provider、真实写锁争用、两 worker 和取消场景验收。

T2 区分“持久数据存在”与“前端能发现、对话能续接”。后续回归需要实际请求 /logistics/case 并确认认证隔离，不应只断言 SQLite 有记录。

T3 暴露业务去重口径。实施前需要定义活动案件和结案后新申请规则；不能为了让测试通过，简单永久禁止同订单再次建案。

T4 用合成轨迹挑战断言器，是对评测本身的测试。后续加入偏序、核验业务成功、授权事件关联和“实际副作用前校验”断言；同时保留合法缓存和幂等回放的豁免规则。

## 4. 已复核与未复核的历史材料

- 本轮读取 backend/data/interview-v010/eval/report.json：flow_pass_semantic_pending=30；45 观察轮次；unexpected_tickets=0；P50/P95=1656/5218ms；逻辑模型调用 53，错误 0；供应商报告输入/输出 token=10716/3263。
- 该报告仍标记语义待人工复核，不是 30 案自然语言答案全部正确的证明。
- 本轮没有重新运行完整 HTTP 模型回放，也没有重新执行冻结说明中的 205 项全量后端和 17 项前端测试。
- P2/P3 的原始报告在当前 DiAn 工作副本的所述路径缺失；已阅读对应版本库文档，但没有把它们当成本轮原始数据复验结果。
- 未进行真实 Redis 故障切换、进程暂停/接管、真实 Provider、正式多租户与生产压测。这些均在主方案中列为待验收事项。
