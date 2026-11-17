"""Evidence measured by the replay runner and its isolated target API."""
import hashlib
import json
import os
import platform
import socket
import sqlite3
import subprocess
from importlib.metadata import distributions
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = 1
MODES = ("rules_only", "llm_rules", "full")
SOURCE_DIRS = ("agents", "api", "core", "evaluation", "memory", "monitor", "tooling")
BOOL_CONFIG = ("intent_embedding", "logistics_embedding", "remote_embedding_available",
               "temporal_logistics", "external_mcp", "authentication")
TEXT_CONFIG = ("intent_mode", "model", "model_endpoint_sha256", "embedding_provider",
               "embedding_model", "ticket_provider")
# Allowlist numeric settings only. Auth credentials, URLs and data paths stay out.
NUMERIC_CONFIG = {
    "intent_timeout_s": ("DIAN_INTENT_TIMEOUT_S", 8.0),
    "agent_timeout_s": ("DIAN_LLM_TIMEOUT_S", 15.0),
    "composer_timeout_s": ("DIAN_COMPOSER_TIMEOUT_S", 8.0),
    "composer_temperature": ("DIAN_COMPOSER_TEMPERATURE", 0.1),
    "composer_max_tokens": ("DIAN_COMPOSER_MAX_TOKENS", 1000),
    "model_timeout_s": ("DIAN_MODEL_TRANSPORT_TIMEOUT_S", 30.0),
    "chat_timeout_s": ("DIAN_CHAT_TIMEOUT_S", 50.0),
    "chat_model_calls": ("DIAN_CHAT_MODEL_CALLS", 16),
    "queue_timeout_s": ("DIAN_QUEUE_TIMEOUT_S", 2.0),
    "chat_per_user": ("DIAN_CHAT_PER_USER", 4),
    "chat_active": ("DIAN_CHAT_ACTIVE", 16),
    "chat_waiting": ("DIAN_CHAT_WAITING", 32),
    "logistics_active": ("DIAN_LOGISTICS_ACTIVE", 4),
    "logistics_waiting": ("DIAN_LOGISTICS_WAITING", 8),
    "model_active": ("DIAN_MODEL_ACTIVE", 8),
    "model_waiting": ("DIAN_MODEL_WAITING", 16),
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def value_digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode())


def build_identity(root):
    modules = {
        path.relative_to(root).as_posix(): sha256(path.read_bytes().replace(b"\r\n", b"\n"))
        for directory in SOURCE_DIRS
        for path in (root / directory).rglob("*.py")
    }
    packages = sorted((dist.metadata["Name"].lower(), dist.version)
                      for dist in distributions() if dist.metadata["Name"])
    return {
        "source_sha256": value_digest(modules),
        "dependencies_sha256": value_digest(packages),
        "python": platform.python_version(),
        "system": platform.system(),
        "architecture": platform.machine(),
        "sqlite": sqlite3.sqlite_version,
    }


def git_commit(root):
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                capture_output=True, text=True, check=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def runtime_snapshot(build, orchestrator, logistics, skills):
    recognizer = orchestrator._intent_recognizer
    embedding = recognizer._embedding
    url = urlsplit(str(recognizer.client.base_url))
    tools = {
        role.value: [{
            name: {"schema": spec.input_schema, "description": spec.description,
                   "allowed_agents": spec.allowed_agents, "retry_safe": spec.retry_safe}
            for name, spec in agent.get_tools().items()
        } for agent in agents]
        for role, agents in orchestrator._pool.items()
    }
    skills_digest = value_digest({
        "max_prompt_chars": skills.max_prompt_chars,
        "loaded": [{key: value for key, value in vars(skill).items() if key != "path"} for skill in skills.skills],
    })
    return {
        "schema": SCHEMA,
        "hostname": socket.gethostname(),
        "identity": {
            "build": build,
            "fixtures_sha256": value_digest(logistics.orders),
            "policy_sha256": value_digest(logistics.rules),
            "skills_sha256": skills_digest,
            "tools_sha256": value_digest(tools),
            "configuration": {
                "intent_mode": recognizer._recognition_mode,
                "model": recognizer.model,
                "model_endpoint_sha256": value_digest([url.scheme, url.hostname, url.port, url.path]),
                "intent_embedding": recognizer._embedding_enabled,
                "logistics_embedding": recognizer._logistics_embedding_enabled,
                "embedding_provider": embedding.provider,
                "remote_embedding_available": embedding.enabled,
                "embedding_model": embedding.MODEL,
                "ticket_provider": "http_mock" if logistics.http_ticket_provider else "sqlite",
                "temporal_logistics": os.getenv("DIAN_TEMPORAL_LOGISTICS", "false").lower() == "true",
                "external_mcp": os.getenv("MCP_EXTERNAL_ENABLED", "true").lower() in {"1", "true", "yes"},
                "authentication": bool(os.getenv("DIAN_API_TOKENS", "").strip()),
                **{key: type(default)(os.getenv(name, str(default))) for key, (name, default) in NUMERIC_CONFIG.items()},
            },
        },
    }


def inspect_image(container, base_url, hostname):
    """Read the target container's immutable local image ID, without inspecting its environment."""
    url = urlsplit(base_url)
    if url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("--container requires a loopback API URL")
    port = url.port or (443 if url.scheme == "https" else 80)
    result = subprocess.run(
        ["docker", "inspect", "--type", "container", "--format",
         "{{json .Image}}\n{{json .Config.Hostname}}\n{{json .NetworkSettings.Ports}}", container],
        capture_output=True, text=True, check=True, timeout=10,
    )
    image, docker_hostname, ports = map(json.loads, result.stdout.splitlines())
    bindings = (binding for group in ports.values() for binding in (group or []))
    if docker_hostname != hostname or not any(binding["HostPort"] == str(port) for binding in bindings):
        raise ValueError("target API hostname or published port does not match the inspected container")
    return image
