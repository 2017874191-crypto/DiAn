"""Optional tracking query available only through an explicitly configured provider."""
from agents.tools import AgentToolSpec
from core.concurrency import finish_blocking_call
from core.kdniao_tracking import configured_tracking


def build_tracking_tools(provider=None):
    provider = provider if provider is not None else configured_tracking()
    if provider is None:
        return {}

    async def lookup(req, args):
        if req.task_goal != "status_lookup":
            return {"success": False, "source": "kdniao", "error": "该工具只用于用户请求的物流实时状态查询"}
        if sum(t["tool_name"] == "query_authorized_tracking" for t in req.tool_attempts) > 1:
            return {"success": False, "source": "kdniao", "error": "本轮已尝试物流查询，请依据已有结果回复，稍后再主动查询"}
        result = await finish_blocking_call(provider.query_logistics, req.user_id, args["order_id"])
        req.tracking_query = result
        return result

    return {"query_authorized_tracking": AgentToolSpec(
        name="query_authorized_tracking",
        description="只读查询当前用户已绑定订单的外部物流轨迹；输入订单号。回复注明来源和查询时间。"
                    "归属拒绝时尚未查询外部接口，不能推断物流状态。不能查询退款、账户状态或执行业务操作。",
        input_schema={"type": "object", "properties": {"order_id": {"type": "string", "minLength": 1}},
                      "required": ["order_id"], "additionalProperties": False},
        handler=lookup, allowed_agents=("general",), capabilities=("status_lookup:logistics",),
    )}
