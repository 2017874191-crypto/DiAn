"""LLM response helpers shared by Anthropic-compatible providers."""
import asyncio
import os
import time
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterable, List
from urllib.parse import urlsplit

from core.concurrency import Capacity, CapacityExceeded
from core.performance import record_event, record_llm_call


class ModelBudgetExceeded(RuntimeError):
    pass


@dataclass
class ModelBudget:
    remaining: int


_scope = ContextVar("model_scope", default=("online", None))
model_capacities: dict[str, Capacity] = {}


def configure_model_capacities():
    model_capacities.update(
        online=Capacity.from_env("DIAN_MODEL", 8, 16),
        profile=Capacity.from_env("DIAN_PROFILE_MODEL", 1, 4),
        evaluation=Capacity.from_env("DIAN_EVAL_MODEL", 2, 4),
    )


@contextmanager
def model_scope(lane="online", *, max_calls=None):
    token = _scope.set((lane, None if max_calls is None else ModelBudget(max_calls)))
    try:
        yield
    finally:
        _scope.reset(token)


async def create_message(client: Any, *, stage: str = "unspecified", **kwargs: Any) -> Any:
    """Call bounded support tasks without DeepSeek's implicit thinking budget.

    DeepSeek enables thinking by default, and those tokens share max_tokens with
    the answer. A short JSON request can therefore finish with thinking only.
    Use extra_body for compatibility with the pinned Anthropic SDK; leave other
    providers and explicitly configured thinking modes unchanged.
    """
    if urlsplit(str(getattr(client, "base_url", ""))).hostname == "api.deepseek.com":
        extra_body = dict(kwargs.get("extra_body") or {})
        if "thinking" not in kwargs and "thinking" not in extra_body:
            extra_body["thinking"] = {"type": "disabled"}
            kwargs["extra_body"] = extra_body
    lane, budget = _scope.get()
    capacity = model_capacities.get(lane)
    try:
        async with capacity.hold() if capacity is not None else nullcontext():
            if budget is not None:
                if budget.remaining <= 0:
                    record_event("model_budget_exhausted")
                    raise ModelBudgetExceeded("本轮模型调用额度已用完")
                budget.remaining -= 1
            return await _send_message(client, stage, kwargs)
    except CapacityExceeded:
        record_event("model_capacity_rejected")
        raise


async def _send_message(client, stage, kwargs):
    started, response, status, error_type = time.monotonic(), None, "error", None
    try:
        async with asyncio.timeout(max(.001, float(os.getenv("DIAN_MODEL_TRANSPORT_TIMEOUT_S", "30")))):
            response = await client.messages.create(**kwargs)
        status = "success"
        return response
    except BaseException as exc:
        error_type = type(exc).__name__
        raise
    finally:
        record_llm_call(stage, kwargs.get("model", "unknown"), started, status, response, error_type=error_type)


def extract_text_content(content: Iterable[Any], separator: str = "\n") -> str:
    """Return text blocks from Anthropic-style response content."""
    texts: List[str] = []
    for block in content or []:
        if isinstance(block, str):
            texts.append(block)
            continue

        block_type = getattr(block, "type", None)
        text = getattr(block, "text", None)
        if isinstance(block, dict):
            block_type = block.get("type", block_type)
            text = block.get("text", text)

        if isinstance(text, str) and (block_type in (None, "text")):
            texts.append(text)

    return separator.join(t for t in texts if t)
