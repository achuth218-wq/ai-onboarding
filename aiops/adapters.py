"""Protocol adapters. Register another adapter without editing workflows."""
from copy import deepcopy

from .common import Tagged, required


def common(resource, env):
    meta, spec = resource["metadata"], resource["spec"]
    access = {"auth_strategies": [env["auth_strategy_id"]]}
    if env["consumer_groups"]:
        access["acls"] = {"allow": env["consumer_groups"]}
    return {
        "ref": meta["name"],
        "name": meta["name"],
        "display_name": meta["name"],
        "enabled": True,
        "labels": {"owner": meta["owner"], "pipeline": "github-actions"},
        "ai_gateway": Tagged("!lookup", {"id": env["gateway_id"]}),
        "access": access,
        "policies": env["policy_profiles"][spec["policy_profile"]],
    }


def route(resource):
    return {"paths": [resource["spec"]["path"]], "protocols": ["https"],
            "strip_path": True, "response_buffering": False}


def llm(resource, env):
    spec = resource["spec"]
    item = common(resource, env)
    provider_name = resource["metadata"]["name"] + "-provider"
    provider = {
        "ref": provider_name, "name": provider_name, "display_name": provider_name,
        "ai_gateway": item["ai_gateway"], "type": spec["provider"],
        "config": {"auth": {"type": "basic", "headers": [{
            "name": spec["credential_header"], "value": Tagged("!secret", {
                "source": Tagged("!env", spec["credential_env"])})}]}},
    }
    item.update({"type": "model", "formats": [{"type": "openai"}],
                 "capabilities": ["generate"],
                 "targets": [{"name": spec["model"], "provider": Tagged("!ref", provider_name + "#name"),
                              "config": {**spec["provider_options"], "type": spec["provider"]}}],
                 "config": {"route": {**route(resource), "model": {
                     "body_param": "model", "values": [resource["metadata"]["name"]]}},
                            "logging": {"payloads": False}}})
    return {"ai_gateway_model_providers": [provider], "ai_gateway_models": [item]}


def mcp(resource, env):
    spec = resource["spec"]
    item = common(resource, env)
    item["access"]["acl_attribute_type"] = "consumer"
    item.update({"type": spec["mode"], "config": {
        "url": env["upstreams"][spec["upstream_ref"]], "route": route(resource),
        "logging": {"payloads": False, "audits": True}, "max_request_body_size": 1048576}})
    if spec["mode"] == "conversion-listener":
        item["tools"] = deepcopy(spec["tools"])
    return {"ai_gateway_mcp_servers": [item]}


def a2a(resource, env):
    spec = resource["spec"]
    item = common(resource, env)
    item.update({"type": "a2a", "config": {
        "url": env["upstreams"][spec["upstream_ref"]], "route": route(resource),
        "logging": {"payloads": False}, "max_request_body_size": 1048576}})
    return {"ai_gateway_agents": [item]}


ADAPTERS = {"LLM": llm, "MCP": mcp, "A2A": a2a}


def render(resources, env):
    result = {"_defaults": {"kongctl": {"namespace": env["namespace"]}}}
    for resource in resources:
        for kind, entries in ADAPTERS[resource["kind"]](resource, env).items():
            result.setdefault(kind, []).extend(entries)
    return result
