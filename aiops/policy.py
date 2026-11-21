"""Platform-owned placement and egress policy; issue intake cannot modify it."""
import ipaddress
from urllib.parse import urlsplit

from .common import required

CLOUDS = ("aws", "azure", "gcp")
EXPOSURES = ("internal", "external")
STAGES = ("dev", "test", "preprod", "prod")
LANES = tuple(f"{c}-{e}" for c in CLOUDS for e in EXPOSURES)
TARGETS = tuple(f"{c}-{e}-{s}" for c in CLOUDS for e in EXPOSURES for s in STAGES)


def lane(value):
    return value["cloud"] + "-" + value["exposure"]


def selected(resources, env):
    return [r for r in resources if lane(r["placement"]) == lane(env)]


def approved_url(url, env):
    u = urlsplit(url)
    required(u.scheme == "https" and u.hostname and not u.username and not u.password
             and not u.query and not u.fragment and u.port in (None, 443),
             "Upstreams must be HTTPS on port 443 without credentials, query or fragment")
    host = u.hostname
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        required(False, "IP-literal upstreams are forbidden; use an approved DNS name")
    required(host not in {"localhost", "metadata.google.internal"} and not host.endswith(".localhost"),
             "Local and metadata endpoints are forbidden")
    required(host in env["allowed_upstream_hosts"], "Upstream host needs platform allowlist approval")


def endpoint_values(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and (key.endswith(("url", "uri", "endpoint", "hostname"))
                                          or "://" in item):
                yield item
            else:
                yield from endpoint_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from endpoint_values(item)


def validate_resource_policy(resource, env):
    spec = resource["spec"]
    required(resource["placement"]["data_classification"] in env["allowed_data_classes"],
             "Data classification is not allowed in this deployment lane")
    required(spec["policy_profile"] in env["policy_profiles"], "Missing policy profile")
    if resource["kind"] == "LLM":
        required(spec["provider"] in env["approved_providers"], "Provider needs platform approval")
        required(spec["credential_env"] not in {"AIOPS_KONGCTL", "AIOPS_SECRET_VALUES"},
                 "Reserved credential variable")
        for url in endpoint_values(spec["provider_options"]):
            approved_url(url, env)
    else:
        required(spec["upstream_ref"] in env["upstreams"], "Missing environment upstream")
        approved_url(env["upstreams"][spec["upstream_ref"]], env)
        # Conversion tool paths must stay relative to the approved server.
        for tool in spec.get("tools", []):
            path = tool.get("path", "")
            required(path.startswith("/") and not path.startswith("//") and "://" not in path
                     and ".." not in path and "\\" not in path, "Unsafe MCP tool path")


def live_controls(env):
    required(env["enabled"], "Deployment target is disabled; platform must configure and enable it")
    required(not env["cloud_scope"].startswith("REPLACE_"), "Set the cloud account/subscription/project identifier")
    controls = env["controls"]
    ingress = "private_ingress_verified" if env["exposure"] == "internal" else "public_edge_verified"
    required(all(controls[k] for k in (ingress, "egress_restricted", "request_limits",
                                     "audit_redaction", "upstream_tls_verified")),
             "Platform security controls are not attested for this target")
    u = urlsplit(env["security_review_url"])
    required(u.scheme == "https" and u.hostname and not u.username and not u.password,
             "Link the platform security review evidence before deployment")


def enabled_lanes(resources, environments):
    return sorted({t.rsplit("-", 1)[0] for t in enabled_targets(resources, environments)})


def enabled_targets(resources, environments):
    return [t for t in TARGETS if environments[t]["enabled"] and selected(resources, environments[t])]
