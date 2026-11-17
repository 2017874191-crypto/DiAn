import json
import sys
from copy import deepcopy

import httpx
import pytest

import api.main as api
from agents.agent_orchestrator import AgentOrchestrator
from core.skill_loader import SkillManager
from evaluation import logistics_eval_runner as runner
from evaluation.compare_logistics_ablation import VARIANTS, compare
from evaluation.provenance import NUMERIC_CONFIG, build_identity, value_digest


def identity(variant):
    build = dict(source_sha256="a" * 64, dependencies_sha256="b" * 64, python="3.12.0",
                 system="Linux", architecture="x86_64", sqlite="3.40.0")
    return {
        "schema": 1, "dataset_sha256": "c" * 64, "controlled_restart": True, "verification_errors": [],
        "case_ids": sorted(case["case_id"] for case in runner.load_cases("dev")),
        "runner": {**build, "git_commit": "d" * 40},
        "target": {
            "image_id": "sha256:" + "a" * 64,
            "runtime": {
                "build": build, "fixtures_sha256": "d" * 64, "policy_sha256": "e" * 64,
                "skills_sha256": "f" * 64, "tools_sha256": "a" * 64,
                "configuration": {
                    "intent_mode": variant, "model": "stub", "model_endpoint_sha256": "b" * 64,
                    "embedding_provider": "local", "intent_embedding": True, "logistics_embedding": True,
                    "embedding_model": "stub", "remote_embedding_available": False,
                    "ticket_provider": "sqlite", "temporal_logistics": False, "external_mcp": False,
                    "authentication": False,
                    **{key: default for key, (_, default) in NUMERIC_CONFIG.items()},
                },
            },
        },
    }


def write_reports(root, mutate_full=lambda _raw: None):
    for variant in VARIANTS:
        cases = [{
            "case_id": case["case_id"], "family_id": case["family_id"], "turns": [],
            "status": "flow_pass_semantic_pending",
        } for case in runner.load_cases("dev")]
        if variant == "full":
            cases[0]["status"] = "model_or_service_failure"
        raw = {**runner.report(cases, "dev", variant), "provenance": identity(variant)}
        if variant == "full":
            mutate_full(raw)
        path = root / variant / "eval" / "report.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(raw), encoding="utf-8")


def test_comparison_keeps_failed_cases_with_measured_metadata(tmp_path):
    write_reports(tmp_path)
    result = compare(tmp_path)
    assert result["image_id"] == "sha256:" + "a" * 64
    assert result["dataset_sha256"] == "c" * 64
    assert result["variants"]["full"]["summary"]["model_or_service_failure"] == 1
    assert result["variants"]["full"]["metrics"]["cases_total"] == 30
    assert result["case_differences"]["llm_to_full_regressed"] == [runner.load_cases("dev")[0]["case_id"]]


@pytest.mark.parametrize("path,changed", [
    (("provenance",), None),
    (("provenance", "dataset_sha256"), "0" * 64),
    (("provenance", "target", "runtime", "build", "source_sha256"), "0" * 64),
    (("provenance", "target", "runtime", "policy_sha256"), "0" * 64),
    (("provenance", "target", "runtime", "configuration", "model"), "other-model"),
    (("provenance", "target", "runtime", "configuration", "intent_mode"), "llm_rules"),
    (("provenance", "target", "image_id"), "sha256:" + "0" * 64),
    (("provenance", "verification_errors"), [{"checkpoint": "after_replay", "reason": "target_identity_changed"}]),
])
def test_comparison_rejects_mixed_or_missing_evidence(tmp_path, path, changed):
    def mutate(raw):
        for key in path[:-1]:
            raw = raw[key]
        raw[path[-1]] = changed

    write_reports(tmp_path, mutate)
    with pytest.raises(ValueError, match="conditions differ|invalid or missing run provenance"):
        compare(tmp_path)


@pytest.mark.asyncio
async def test_api_runtime_measures_loaded_assets_without_exposing_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DIAN_LOGISTICS_DB", str(tmp_path / "runtime.sqlite3"))
    monkeypatch.setenv("DIAN_TICKET_PROVIDER", "sqlite")
    monkeypatch.setenv("INTENT_RECOGNITION_MODE", "rules_only")
    monkeypatch.setenv("INTENT_EMBEDDING_PROVIDER", "local")
    admin = "test-admin-token-at-least-24"
    reader = "test-user-token-at-least-24"
    monkeypatch.setenv("DIAN_API_TOKENS", json.dumps({
        admin: {"user_id": "admin", "role": "admin"},
        reader: {"user_id": "reader", "role": "user"},
    }))
    orch = AgentOrchestrator(api_key="secret-provider-key", model="stub",
                            base_url="https://account:secret-password@example.invalid/v1?secret-query=yes")
    skills = SkillManager(str(runner.ROOT / "skills"))
    skills.load()
    monkeypatch.setattr(api, "_orchestrator", orch)
    monkeypatch.setattr(api, "_skill_manager", skills)
    monkeypatch.setattr(api.app.state, "eval_runtime_build", None, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {admin}"}
        assert (await client.get("/eval/runtime", headers=headers)).status_code == 404
        api.app.state.eval_runtime_build = build_identity(runner.ROOT)
        assert (await client.get("/eval/runtime", headers={"Authorization": f"Bearer {reader}"})).status_code == 403
        response = await client.get("/eval/runtime", headers=headers)
        assert response.status_code == 200
        first = response.json()["identity"]
        assert first["configuration"]["intent_mode"] == "rules_only"
        assert first["configuration"]["model"] == "stub"
        assert all(secret not in response.text for secret in ("secret-provider-key", "secret-password",
                                                              "secret-query", admin, reader, str(tmp_path)))
        service = api._logistics_service()
        assert first["fixtures_sha256"] == value_digest(service.orders)
        service.rules["test_change"] = True
        new = (await client.get("/eval/runtime", headers=headers)).json()["identity"]
        assert new["policy_sha256"] != first["policy_sha256"]
    await orch._intent_recognizer.client.close()
    await orch._composer._client.close()


def test_replay_retains_result_but_is_invalid_when_target_changes(tmp_path, monkeypatch):
    db, output = tmp_path / "cases.sqlite3", tmp_path / "report.json"
    db.touch()
    case = runner.load_cases("dev")[0]
    target = identity("rules_only")["target"]
    setup = case["setup"]
    runtime = target["runtime"]
    runtime["fixtures_sha256"] = value_digest(json.loads((runner.ROOT / setup["fixtures_file"]).read_bytes())["orders"])
    runtime["policy_sha256"] = value_digest(json.loads((runner.ROOT / setup["policy_file"]).read_bytes()))
    modified = deepcopy(target)
    modified["runtime"]["policy_sha256"] = "a" * 64
    readings = iter((target, modified))
    monkeypatch.setattr(runner, "read_target", lambda *_args: next(readings))
    monkeypatch.setattr(runner, "run_cases", lambda *_args, **_kwargs: [
        {"case_id": case["case_id"], "family_id": case["family_id"], "turns": [], "status": "model_or_service_failure"},
    ])
    monkeypatch.setattr(sys, "argv", ["replay", "--variant", "rules_only", "--case-id", case["case_id"],
                                     "--base-url", "http://127.0.0.1:1", "--db", str(db), "--output", str(output)])
    assert runner.main() == 1
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["summary"] == {"model_or_service_failure": 1}
    assert result["provenance"]["target"] == target
    assert result["provenance"]["verification_errors"] == [
        {"checkpoint": "after_replay", "reason": "target_identity_changed"},
    ]
