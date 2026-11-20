import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import jsonschema
import yaml

from .adapters import render
from .common import Dumper, Error, digest, plain, read_yaml, required, version, write_json

ROOT = Path(__file__).resolve().parents[1]


def validate_schema(value, name):
    schema = json.loads((ROOT / "schemas" / name).read_text())
    try:
        jsonschema.Draft202012Validator(schema).validate(value)
    except jsonschema.ValidationError as exc:
        raise Error(f"{name}: {'/'.join(map(str, exc.absolute_path))}: {exc.message[:350]}") from None


def validate_url(url, live=False):
    u = urlsplit(url)
    required(u.scheme == "https" and u.hostname and not u.username and not u.password
             and not u.query and not u.fragment, "URLs must be HTTPS with no credentials, query or fragment")
    if live:
        required(not (u.hostname.endswith((".invalid", ".example", ".example.com"))
                      or u.hostname == "example.com"), "Replace example endpoint before deployment")


def load_source(root):
    root = Path(root)
    resources = [read_yaml(p) for p in sorted((root / "catalog").rglob("*.yaml"))]
    required(resources, "Catalog is empty")
    names, routes = set(), []
    for resource in resources:
        validate_schema(resource, "onboarding.json")
        name, path = resource["metadata"]["name"], resource["spec"]["path"]
        required(name not in names, f"Duplicate resource name: {name}")
        names.add(name)
        required(not any(path == p or path.startswith(p + "/") or p.startswith(path + "/")
                         for p in routes), f"Overlapping route: {path}")
        routes.append(path)
        spec = resource["spec"]
        if resource["kind"] == "MCP":
            required(spec["mode"] != "conversion-listener" or spec.get("tools"), "Conversion MCP requires tools")
        if resource["kind"] == "LLM":
            required(name + "-provider" not in names, "Generated provider name collides")
            names.add(name + "-provider")
    environments = {}
    for name in ("dev", "staging", "prod"):
        env = read_yaml(root / "environments" / (name + ".yaml"))
        validate_schema(env, "environment.json")
        required(env["product"] == "kong-ai-gateway", "Kong Gateway 2.3 is unsupported; use AI Gateway 2.3")
        required(version(env["runtime_version"])[:2] == (2, 3), "This adapter is certified for AI Gateway 2.3.x")
        validate_url(env["proxy_url"])
        for url in env["upstreams"].values():
            validate_url(url)
        for resource in resources:
            spec = resource["spec"]
            required(spec["policy_profile"] in env["policy_profiles"], "Missing policy profile")
            required(spec.get("upstream_ref", "") in env["upstreams"] or resource["kind"] == "LLM",
                     "Missing environment upstream")
        environments[name] = env
    return resources, environments


def validate_native(document):
    for key, entries in document.items():
        if key.startswith("_"):
            continue
        for entry in entries:
            validate_schema(plain(entry), "kongctl-1.28.4/" + key + ".json")


def build(source, output, revision):
    required(re.fullmatch(r"[a-f0-9]{40}", revision) or revision == "local", "Revision must be a full commit SHA")
    resources, environments = load_source(source)
    output = Path(output)
    required(not output.exists() or not any(output.iterdir()), "Build directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "catalog.json", resources)
    for name, env in environments.items():
        document = render(resources, env)
        validate_native(document)
        directory = output / name
        directory.mkdir()
        (directory / "kong.yaml").write_text(yaml.dump(document, Dumper=Dumper, sort_keys=False))
        write_json(directory / "environment.json", env)
    files = {str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(output.rglob("*")) if p.is_file()}
    metadata = {"format": 1, "source_revision": revision, "kongctl_version": "1.28.4", "files": files}
    metadata["bundle_digest"] = digest(metadata)
    write_json(output / "bundle.json", metadata)
    return metadata["bundle_digest"]


def verify_bundle(bundle):
    bundle = Path(bundle)
    required(not (bundle / "bundle.json").is_symlink(), "Unsafe bundle metadata path")
    meta = json.loads((bundle / "bundle.json").read_text())
    required(meta.get("format") == 1 and meta.get("kongctl_version") == "1.28.4", "Unsupported bundle format or toolchain")
    expected = meta.pop("bundle_digest")
    required(digest(meta) == expected, "Bundle metadata digest mismatch")
    actual = {str(p.relative_to(bundle)) for p in bundle.rglob("*") if p.is_file()}
    required(actual == set(meta["files"]) | {"bundle.json"}, "Bundle file list mismatch")
    for relative, checksum in meta["files"].items():
        path = bundle / relative
        required(not path.is_symlink() and path.resolve().is_relative_to(bundle.resolve()), "Unsafe bundle path")
        required(hashlib.sha256(path.read_bytes()).hexdigest() == checksum, f"Bundle changed: {relative}")
    return expected


def deployment_inputs(bundle, environment):
    verify_bundle(bundle)
    required(environment in ("dev", "staging", "prod"), "Unknown environment")
    env = json.loads((Path(bundle) / environment / "environment.json").read_text())
    required(env["gateway_id"] != "00000000-0000-0000-0000-000000000000", "Set the real AI Gateway ID")
    required(env["auth_strategy_id"] != "00000000-0000-0000-0000-000000000000", "Set the inbound auth strategy ID")
    validate_url(env["proxy_url"], live=True)
    for url in env["upstreams"].values():
        validate_url(url, live=True)
    required(all(env["policy_profiles"].values()), "Every policy profile needs existing Kong policy IDs or names")
    required(env["consumer_groups"], "Configure an authorized consumer group for these routes")
    return env
