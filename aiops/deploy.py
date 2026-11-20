import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from .build import deployment_inputs, verify_bundle
from .common import Error, digest, required, version, write_json


def load_secret_values():
    values = json.loads(os.environ.get("AIOPS_SECRET_VALUES", "{}"))
    required(isinstance(values, dict), "AIOPS_SECRET_VALUES must be a JSON object")
    for key, value in values.items():
        required(key.startswith("AIOPS_") and key not in ("AIOPS_SECRET_VALUES", "AIOPS_KONGCTL")
                 and isinstance(value, str) and value, "Invalid secret mapping")
        os.environ[key] = value


def command(arguments, env, json_output=False):
    state = Path(".state")
    state.mkdir(exist_ok=True, mode=0o700)
    execution_env = dict(os.environ)
    execution_env["KONGCTL_DEFAULT_KONNECT_PAT"] = os.environ.get("KONNECT_TOKEN", "")
    execution_env["KONGCTL_DEFAULT_KONNECT_REGION"] = env["region"]
    required(execution_env["KONGCTL_DEFAULT_KONNECT_PAT"], "KONNECT_TOKEN is required")
    cmd = [os.environ.get("AIOPS_KONGCTL", "kongctl"), "--no-telemetry",
           "--log-file", str(state / "kongctl.log"), *arguments]
    result = subprocess.run(cmd, env=execution_env, capture_output=True, text=True, timeout=600)
    if result.returncode:
        # API and CLI diagnostics can contain sensitive fields. Never export them to CI logs.
        raise Error(f"kongctl {arguments[0]} failed (exit {result.returncode}); check permissions, target and schema. "
                    "Raw API output is intentionally not published.")
    return json.loads(result.stdout) if json_output else result.stdout


def array_response(response):
    if isinstance(response, list):
        return response
    required(isinstance(response, dict) and isinstance(response.get("data"), list), "Unexpected node-list response")
    return response["data"]


def check_nodes(nodes, env, expected_config=None, now=None):
    now = time.time() if now is None else now
    required(len(nodes) >= env["minimum_nodes"], "Insufficient registered data planes")
    for node in nodes:
        required(version(node["version"]) == version(env["runtime_version"]), "Runtime version does not match the pinned AI Gateway version")
        heartbeat = float(node.get("last_ping", 0))
        if heartbeat > 10**12:
            heartbeat /= 1000
        required(-30 <= now - heartbeat <= env["heartbeat_max_age_seconds"], "Data-plane heartbeat is stale")
        required(not node.get("errors"), "Data plane reports configuration errors")
        compatibility = node.get("compatibility_status") or {}
        required(not compatibility.get("issues"), "Data plane reports compatibility issues")
        required(str(compatibility.get("state", "")).lower() not in
                 ("incompatible", "error", "invalid", "unsupported"), "Incompatible data plane")
        if expected_config is not None:
            required(str(node.get("config_version")) == str(expected_config), "Data plane has not received the expected configuration")


def preflight(env):
    cli_version = command(["version"], env).strip()
    required(cli_version == "1.28.4", "Install kongctl 1.28.4")
    gateway = command(["get", "ai-gateway", env["gateway_id"], "-o", "json"], env, True)
    required(gateway.get("id") == env["gateway_id"], "Wrong AI Gateway returned")
    required(version(gateway.get("min_runtime_version", "0.0")) >= (2, 3, 0),
             "Set this AI Gateway's min_runtime_version to 2.3 after upgrading its data planes")
    nodes = array_response(command(["get", "ai-gateway", "nodes", "--gateway-id", env["gateway_id"], "-o", "json"], env, True))
    check_nodes(nodes, env)
    return gateway


def normalize_plan(plan):
    value = json.loads(json.dumps(plan))
    value["metadata"].pop("generated_at", None)
    return value


def assert_plan(plan, namespace):
    required(plan.get("metadata", {}).get("mode") == "apply", "Only create/update plans are accepted")
    for change in plan["changes"]:
        required(change["action"].lower() in ("create", "update"), "Deletion is forbidden in onboarding")
        required(change.get("namespace") == namespace, "Plan crosses its ownership namespace")
        required(change["resource_type"] in {"ai_gateway_model_provider", "ai_gateway_model",
                 "ai_gateway_mcp_server", "ai_gateway_agent"}, "Unexpected resource type in plan")


def make_plan(bundle, environment, env, path):
    command(["plan", "--mode", "apply", "-f", str(Path(bundle) / environment / "kong.yaml"),
             "--output-file", str(path), "--require-namespace", env["namespace"]], env)
    plan = json.loads(Path(path).read_text())
    assert_plan(plan, env["namespace"])
    return plan


def review_plan(bundle, environment, output):
    env = deployment_inputs(bundle, environment)
    preflight(env)
    with tempfile.TemporaryDirectory() as temp:
        plan = make_plan(bundle, environment, env, Path(temp) / "plan.json")
    receipt = {
        "bundle_digest": verify_bundle(bundle), "environment": environment,
        "plan_digest": digest(normalize_plan(plan)), "changes": [
            {"action": c["action"], "resource_type": c["resource_type"], "resource_ref": c["resource_ref"],
             "changed_fields": sorted(c.get("changed_fields", c.get("fields", {})))} for c in plan["changes"]],
    }
    write_json(output, receipt)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"### {environment} deployment review\n\nBundle: `{receipt['bundle_digest']}`\n\n"
                    f"Plan: `{receipt['plan_digest']}`\n\nReview the immutable rendered configuration artifact for values.\n\n")
            f.write("| Action | Type | Resource | Fields |\n|---|---|---|---|\n")
            for c in receipt["changes"]:
                f.write(f"| {c['action']} | {c['resource_type']} | {c['resource_ref']} | {', '.join(c['changed_fields'])} |\n")
            if not receipt["changes"]:
                f.write("\nNo configuration changes.\n")
    return receipt


def wait_ready(env):
    target = command(["get", "ai-gateway", env["gateway_id"], "-o", "json"], env, True).get("config_version")
    required(target is not None, "Control plane did not expose config_version; refusing to infer readiness")
    end = time.monotonic() + env["readiness_timeout_seconds"]
    while True:
        nodes = array_response(command(["get", "ai-gateway", "nodes", "--gateway-id", env["gateway_id"], "-o", "json"], env, True))
        try:
            check_nodes(nodes, env, target)
            return target
        except Error:
            if time.monotonic() >= end:
                raise Error("Data planes did not converge within the readiness timeout") from None
            time.sleep(5)


def apply_plan(bundle, environment, expected_digest, output):
    from .smoke import smoke
    env = deployment_inputs(bundle, environment)
    load_secret_values()
    resources = json.loads((Path(bundle) / "catalog.json").read_text())
    for resource in resources:
        if resource["kind"] == "LLM":
            required(os.environ.get(resource["spec"]["credential_env"]),
                     "A required model-provider credential is missing")
    required(os.environ.get("SMOKE_AUTH_VALUE"), "SMOKE_AUTH_VALUE is required before deployment")
    preflight(env)
    receipt = {"environment": environment, "bundle_digest": verify_bundle(bundle), "status": "not-applied"}
    try:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fresh-plan.json"
            plan = make_plan(bundle, environment, env, path)
            required(digest(normalize_plan(plan)) == expected_digest,
                     "Plan changed after review; rerun the workflow and approve the new plan")
            receipt["status"] = "apply-started"
            write_json(output, receipt)
            command(["apply", "--plan", str(path), "--auto-approve", "--require-namespace", env["namespace"]], env)
        receipt["status"] = "applied-awaiting-verification"
        receipt["config_version"] = wait_ready(env)
        receipt["checks"] = smoke(bundle, environment)
        receipt["status"] = "verified"
        write_json(output, receipt)
        return receipt
    except Exception:
        write_json(output, receipt)
        raise
