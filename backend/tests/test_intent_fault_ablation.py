import httpx
import pytest

from evaluation.compare_intent_faults import compare
from evaluation.logistics_eval_runner import close_fixture_case


@pytest.mark.asyncio
async def test_fixed_fault_replays_preserve_business_and_cleanup_boundaries():
    result = await compare()
    for profiles in result["scenarios"].values():
        for profile, cases in profiles.items():
            for case_id, row in cases.items():
                if profile == "healthy":
                    assert row["status"] == "flow_pass_semantic_pending", (case_id, row)
                else:
                    assert {"turn": 0, "stage": "intent", "outcome": "connect_error", "count": 1} in row["model_attempts"]
                assert all(not turn["unexpected_tickets"] for turn in row["turns"])
                if case_id != "DIAN-025":
                    assert row["cases_created"] == row["ticket_count"] == 0
                elif row["status"] != "flow_pass_semantic_pending":
                    assert row["ticket_count"] == 0

    closures = result["cleanup_disconnects"]
    for fault in ("before", "after"):
        old = closures[fault]["single_attempt"]
        fixed = closures[fault]["bounded_retry"]
        assert old["status"] == "environment_failure"
        assert old["cleanup"]["closed_cases"] == int(fault == "after")
        assert fixed["status"] == "flow_pass_semantic_pending"
        assert fixed["ticket_count"] == fixed["cleanup"]["closed_cases"] == 1
        assert fixed["cleanup"]["attempts"] == 2
        assert fixed["cleanup"]["committed_responses"] == (2 if fault == "after" else 1)
        assert fixed["cleanup"]["distinct_closed_at"] == 1
        assert fixed["cleanup"]["runner_observations"] == [
            {"status": "closed", "attempts": 2, "transport_errors": ["ReadError"]},
        ]

    failed = closures["persistent"]["bounded_retry"]
    assert failed["status"] == "environment_failure"
    assert failed["cleanup"]["closed_cases"] == 0
    assert failed["ticket_count"] == 1
    assert failed["cleanup"]["runner_observations"] == [
        {"status": "transport_failure", "attempts": 2, "transport_errors": ["ReadError", "ReadError"]},
    ]


def test_cleanup_does_not_retry_an_http_rejection():
    requests = []

    def denied(request):
        requests.append(request)
        return httpx.Response(403)

    with httpx.Client(transport=httpx.MockTransport(denied), base_url="http://fixture") as client:
        assert close_fixture_case(client, "case", "user", "conv") == {
            "status": "http_failure", "attempts": 1, "transport_errors": [], "http_status": 403,
        }
    assert len(requests) == 1
