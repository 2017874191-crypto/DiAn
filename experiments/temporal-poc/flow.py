"""Deterministic orchestration only. External work lives in activities."""
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class TicketWorkflow:
    @workflow.run
    async def run(self, payload: dict) -> dict:
        return await workflow.execute_activity(
            'execute_ticket', payload,
            start_to_close_timeout=timedelta(seconds=4),
            schedule_to_close_timeout=timedelta(seconds=35),
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_interval=timedelta(seconds=2), maximum_attempts=5),
        )
