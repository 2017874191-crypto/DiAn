"""Separate intent-goal and sourced-answer changes through the existing ASGI replay."""
import argparse
import ast
import asyncio
import json
import logging
import shutil
import subprocess
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from agents import agent_orchestrator
from agents.agent_orchestrator import BaseAgent, GeneralAgent
from core import intent_recognizer
from core.skill_loader import SkillManager
from evaluation import replay_logistics_candidate as runner
from evaluation.prepare_rag_data import ROOT, write_json
from evaluation.provenance import git_commit, sha256


def committed_text(revision, path):
    return subprocess.check_output(["git", "show", revision + ":backend/" + path], cwd=ROOT).decode("utf-8")


def old_texts(revision):
    intent_source = committed_text(revision, "core/intent_recognizer.py")
    tree = ast.parse(intent_source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "IntentRecognizer")
    method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_llm_recognize")
    # Use current enum objects so the restored method composes with the actual orchestrator.
    namespace = vars(intent_recognizer).copy()
    exec(compile(ast.Module(body=[method], type_ignores=[]), "<committed-intent-method>", "exec"), namespace)
    agent_source = committed_text(revision, "agents/agent_orchestrator.py")
    base = next(node for node in ast.parse(agent_source).body if isinstance(node, ast.ClassDef) and node.name == "BaseAgent")
    base_method = next(node for node in base.body if isinstance(node, ast.FunctionDef) and node.name == "_build_system_prompt")
    base_namespace = vars(agent_orchestrator).copy()
    exec(compile(ast.Module(body=[base_method], type_ignores=[]), "<committed-system-prompt-method>", "exec"), base_namespace)
    agent = next(node for node in ast.parse(agent_source).body if isinstance(node, ast.ClassDef) and node.name == "GeneralAgent")
    assignment = next(node for node in agent.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "system_prompt" for target in node.targets))
    return namespace["_llm_recognize"], ast.literal_eval(assignment.value), intent_source, base_namespace["_build_system_prompt"], ast.unparse(base_method)


async def compare(protocol_path, cases_path, output):
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    revision = protocol["baseline_commit"]
    old_method, old_prompt, old_intent_source, old_base_method, old_base_source = old_texts(revision)
    current_base_method = BaseAgent._build_system_prompt
    current_base = next(node for node in ast.parse(
        (ROOT / "agents/agent_orchestrator.py").read_text(encoding="utf-8")).body
        if isinstance(node, ast.ClassDef) and node.name == "BaseAgent")
    current_base_source = ast.unparse(next(node for node in current_base.body
        if isinstance(node, ast.FunctionDef) and node.name == "_build_system_prompt"))
    current_method, current_prompt = intent_recognizer.IntentRecognizer._llm_recognize, GeneralAgent.system_prompt
    result = {"schema": 1, "candidate_commit": git_commit(ROOT), "protocol": protocol,
              "protocol_sha256": sha256(protocol_path.read_bytes()),
              "dataset_sha256": sha256(cases_path.read_bytes()), "runs": {}, "complete": False,
              "scope": "One sequential three-group development ablation, live models and actual ASGI /chat",
              "source_limit": "Old text restored into current runtime, not a separately deployed historical build"}
    with tempfile.TemporaryDirectory(prefix="dian-consultation-") as folder:
        old_skills = Path(folder) / "old-skills"
        shutil.copytree(ROOT / "skills", old_skills)
        skill_relative = "skills/general_customer_service/SKILL.md"
        old_skill_text = committed_text(revision, skill_relative)
        (old_skills / "general_customer_service/SKILL.md").write_text(old_skill_text, encoding="utf-8")
        for group in protocol["api_ablation"]["groups"]:
            intent_method = old_method if group == "baseline" else current_method
            prompt = current_prompt if group == "full" else old_prompt
            skills_dir = ROOT / "skills" if group == "full" else old_skills
            run_output = Path(folder) / (group + ".json")
            with ExitStack() as stack:
                stack.enter_context(patch.object(intent_recognizer.IntentRecognizer, "_llm_recognize", intent_method))
                stack.enter_context(patch.object(GeneralAgent, "system_prompt", prompt))
                stack.enter_context(patch.object(BaseAgent, "_build_system_prompt", current_base_method if group == "full" else old_base_method))
                stack.enter_context(patch.object(runner, "SkillManager", lambda _path: SkillManager(str(skills_dir))))
                await runner.replay(run_output, cases_path)
            run = json.loads(run_output.read_text(encoding="utf-8"))
            run["effective_texts"] = {
                "intent_source_sha256": sha256((old_intent_source if group == "baseline" else
                    (ROOT / "core/intent_recognizer.py").read_text(encoding="utf-8")).replace("\r\n", "\n").encode()),
                "general_system_prompt_sha256": sha256(prompt.encode()),
                "general_skill_sha256": sha256((old_skill_text if group != "full" else
                    (ROOT / skill_relative).read_text(encoding="utf-8")).replace("\r\n", "\n").encode()),
                "base_prompt_method_sha256": sha256((old_base_source if group != "full" else current_base_source).encode()),
            }
            result["runs"][group] = run
            write_json(output, result)
            print(group, run["summary"], flush=True)
    result["complete"] = True
    write_json(output, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    base = ROOT / "evaluation/reviews/dian_logistics_v1"
    parser.add_argument("--protocol", type=Path, default=base / "consultation-v2-protocol.json")
    parser.add_argument("--cases-file", type=Path, default=base / "consultation_v2_dev.jsonl")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(compare(args.protocol, args.cases_file, args.output))


if __name__ == "__main__":
    main()
