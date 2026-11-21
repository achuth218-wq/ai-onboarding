"""Strict Markdown issue-form decoder. All submitted values remain data."""
import json
import re
import shutil
import tempfile
from pathlib import Path

import yaml

from .adapters import render
from .build import load_source, validate_native, validate_schema, validate_url
from .common import Error, read_yaml, required
from .policy import STAGES, lane

CONFIRMATIONS = (
    "I have included no credentials, tokens, personal data, or confidential payloads.",
    "The platform team has approved the provider or upstream hosts for this lane.",
    "I understand that this request creates a draft PR and does not deploy immediately.",
)
COMMON = {"Resource type", "Exposure", "Cloud", "Resource name", "Owner team", "Route prefix",
          "Data classification", "Security confirmations"}
FIELDS = {
    "LLM": {"Provider", "Model", "Credential header", "Provider options JSON"},
    "MCP": {"Development upstream URL", "Test upstream URL", "Preprod upstream URL", "Production upstream URL",
            "MCP mode", "MCP protocol version", "Expected tool names", "Conversion tools JSON"},
    "A2A": {"Development upstream URL", "Test upstream URL", "Preprod upstream URL", "Production upstream URL",
            "Expected skill names"},
}


def sections(body):
    required(isinstance(body, str) and len(body.encode()) <= 32768, "Issue body is missing or too large")
    fields, current, fenced = {}, None, False
    for line in body.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("### "):
            current = line[4:].strip()
            required(current not in fields, "Duplicate issue-form field")
            fields[current] = []
        elif current is not None:
            fields[current].append(line)
        else:
            required(not line.strip(), "Unexpected content before issue form")
    required(not fenced, "Unclosed code block in issue form")
    return {k: "\n".join(v).strip() for k, v in fields.items()}


def json_field(text, default):
    if text in ("", "_No response_"):
        return default
    if text.startswith("```"):
        lines = text.splitlines()
        required(lines[0] in ("```json", "```") and lines[-1] == "```", "Expected JSON code block")
        text = "\n".join(lines[1:-1])
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            required(key not in obj, "Duplicate JSON key")
            obj[key] = value
        return obj
    try:
        return json.loads(text, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(Error("Invalid JSON number")))
    except (ValueError, RecursionError):
        raise Error("Invalid JSON in form") from None


def names(text):
    values = [s.strip() for s in text.splitlines() if s.strip()]
    required(values and len(values) <= 100 and len(values) == len(set(values)), "Names must be nonempty and unique")
    required(all(re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", v) for v in values), "Invalid tool or skill name")
    return values


def decode(body):
    fields = sections(body)
    kind = fields.get("Resource type")
    required(kind in FIELDS, "Select an LLM, MCP or A2A onboarding form")
    required(set(fields) == COMMON | FIELDS[kind], "Missing or unknown issue-form fields")
    confirmations = fields["Security confirmations"].splitlines()
    required(confirmations == ["- [X] " + v for v in CONFIRMATIONS]
             or confirmations == ["- [x] " + v for v in CONFIRMATIONS], "Complete all security confirmations")
    resource = {"apiVersion": "platform.example.com/v1alpha1", "kind": kind,
                "metadata": {"name": fields["Resource name"], "owner": fields["Owner team"]},
                "placement": {"cloud": fields["Cloud"], "exposure": fields["Exposure"],
                              "data_classification": fields["Data classification"]},
                "spec": {"path": fields["Route prefix"], "policy_profile": kind.lower() + "-standard"}}
    spec = resource["spec"]
    upstreams = {}
    if kind == "LLM":
        spec.update(provider=fields["Provider"], model=fields["Model"],
                    credential_env="AIOPS_LLM_AUTH_HEADER", credential_header=fields["Credential header"],
                    provider_options=json_field(fields["Provider options JSON"], {}),
                    smoke={"prompt": "Reply with the word ready.", "stream": True})
    else:
        spec["upstream_ref"] = fields["Resource name"]
        upstreams = dict(zip(STAGES, [fields[k + " upstream URL"] for k in ("Development", "Test", "Preprod", "Production")]))
        for url in upstreams.values():
            validate_url(url, live=True)
        if kind == "MCP":
            spec.update(mode=fields["MCP mode"], protocol_version=fields["MCP protocol version"],
                        smoke={"expected_tools": names(fields["Expected tool names"])})
            tools = json_field(fields["Conversion tools JSON"], [])
            required(isinstance(tools, list), "Conversion tools must be a JSON array")
            if tools:
                required(spec["mode"] == "conversion-listener", "Tools are only accepted for conversion MCP")
                spec["tools"] = tools
        else:
            spec.update(protocol_version="0.3.0", smoke={"expected_skills": names(fields["Expected skill names"])})
    try:
        validate_schema(resource, "onboarding.json")
    except Error:
        # Avoid including arbitrary issue text (or a pasted credential) in runner logs.
        raise Error("Form does not match the manifest schema; check names, placement and protocol fields") from None
    return resource, upstreams


def candidate_files(root, body):
    root = Path(root)
    resource, upstreams = decode(body)
    relative = f"catalog/{resource['kind'].lower()}/{resource['metadata']['name']}.yaml"
    required(not (root / relative).exists(), "Resource already exists; update it through a normal pull request")
    changes = {relative: yaml.safe_dump(resource, sort_keys=False)}
    for stage, url in upstreams.items():
        path = f"environments/{lane(resource['placement'])}-{stage}.yaml"
        env = read_yaml(root / path)
        ref = resource["spec"]["upstream_ref"]
        required(ref not in env["upstreams"] or env["upstreams"][ref] == url,
                 "Upstream reference already points elsewhere; intake cannot change shared upstreams")
        env["upstreams"][ref] = url
        changes[path] = yaml.safe_dump(env, sort_keys=False)
    # Validate the complete proposed catalog and all native schemas without executing it.
    with tempfile.TemporaryDirectory() as tmp:
        candidate = Path(tmp)
        for directory in ("catalog", "environments"):
            shutil.copytree(root / directory, candidate / directory)
        for path, text in changes.items():
            (candidate / path).write_text(text)
        try:
            resources, envs = load_source(candidate)
            for env in envs.values():
                validate_native(render(resources, env))
        except Error:
            raise Error("Candidate failed schema or platform policy checks; verify approved hosts, data class, provider, routes and tool configuration") from None
    return resource, changes
