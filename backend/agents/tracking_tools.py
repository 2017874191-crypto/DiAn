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
                    "归属拒绝只做了本地校验，尚未查询外部接口，不得称已查物流；仅建议核对订单号、账号或由服务端核实归属，"
                    "不得索取电话尾号等无法解除归属限制的信息。超时只表示未取得结果，不能推断物流状态，"
                    "请用户稍后主动发起新查询，不得承诺或提议后台自动重试、定时查询。"
                    "模拟或历史回放必须注明 query_mode 对应来源，不得称本次真实查询，未提供 queried_at 不得编造查询时间。"
                    "不能查询退款、账户状态或执行业务操作。",
        input_schema={"type": "object", "properties": {"order_id": {"type": "string", "minLength": 1}},
                      "required": ["order_id"], "additionalProperties": False},
        handler=lookup, allowed_agents=("general",), capabilities=("status_lookup:logistics",),
    )}
