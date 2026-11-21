import copy
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from aiops.adapters import render
from aiops.build import ROOT, build, load_source
from aiops.common import Error, read_yaml
from aiops.issue_forms import CONFIRMATIONS, candidate_files, decode
from aiops.policy import approved_url, enabled_lanes, live_controls, validate_resource_policy

module = importlib.util.spec_from_file_location("intake", ROOT / "scripts/intake_issue.py")
intake = importlib.util.module_from_spec(module)
module.loader.exec_module(intake)


def form_body(kind="LLM", exposure="internal", cloud="aws", overrides=None):
    form = read_yaml(ROOT / f".github/ISSUE_TEMPLATE/{kind.lower()}-{exposure}.yml")
    values = {"Resource type": kind, "Exposure": exposure, "Cloud": cloud,
              "Resource name": "new-resource", "Owner team": "ai-platform", "Route prefix": "/new/resource",
              "Data classification": "public", "Provider": "openai", "Model": "gpt-4o",
              "Credential header": "Authorization", "Provider options JSON": "{}",
              "MCP mode": "passthrough-listener", "MCP protocol version": "2025-11-25",
              "Expected tool names": "status\nhealth", "Expected skill names": "answer", "Conversion tools JSON": "[]",
              "Security confirmations": "\n".join("- [X] " + x for x in CONFIRMATIONS)}
    for stage in ("Development", "Test", "Preprod", "Production"):
        values[stage + " upstream URL"] = f"https://{stage.lower()}.approved.corp/service"
    values.update(overrides or {})
    parts = []
    for field in form["body"]:
        if field["type"] == "markdown":
            continue
        attributes = field["attributes"]
        label = attributes["label"]
        value = values[label]
        if attributes.get("render"):
            value = "```" + attributes["render"] + "\n" + value + "\n```"
        parts.append("### " + label + "\n\n" + value)
    return "\n\n".join(parts)


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for directory in ("catalog", "environments"):
            shutil.copytree(ROOT / directory, self.root / directory)
        for path in (self.root / "environments").glob("*.yaml"):
            env = read_yaml(path)
            env["allowed_upstream_hosts"] += [f"{s}.approved.corp" for s in ("development", "test", "preprod", "production")]
            path.write_text(yaml.safe_dump(env, sort_keys=False))

    def tearDown(self):
        self.temp.cleanup()

    def test_all_six_forms_across_three_clouds_create_only_scoped_files(self):
        for kind in ("LLM", "MCP", "A2A"):
            for exposure in ("internal", "external"):
                for cloud in ("aws", "azure", "gcp"):
                    with self.subTest(kind=kind, exposure=exposure, cloud=cloud):
                        resource, changes = candidate_files(self.root, form_body(kind, exposure, cloud))
                        self.assertEqual(resource["placement"]["cloud"], cloud)
                        self.assertEqual(len(changes), 1 if kind == "LLM" else 5)
                        for path, content in changes.items():
                            if path.startswith("environments/"):
                                self.assertTrue(path.startswith(f"environments/{cloud}-{exposure}-"))
                                proposed = yaml.safe_load(content)
                                original = read_yaml(self.root / path)
                                self.assertEqual(proposed["region"], "us")
                                proposed.pop("upstreams"); original.pop("upstreams")
                                self.assertEqual(proposed, original)

    def test_duplicate_missing_unknown_headings_rejected(self):
        body = form_body()
        for altered in (body + "\n\n### Cloud\n\ngcp", body + "\n\n### Command\n\nid", body.replace("### Cloud", "### Invalid")):
            with self.assertRaises(Error):
                decode(altered)

    def test_traversal_and_invalid_cloud_rejected_without_echoing_input(self):
        for field, value in [("Resource name", "../../.github/workflows/exploit"), ("Cloud", "aws\nrun: evil"),
                             ("Owner team", "$(cat /secret)"), ("Exposure", "public")]:
            with self.subTest(field=field), self.assertRaises(Error) as raised:
                decode(form_body(overrides={field: value}))
            self.assertNotIn(value, str(raised.exception))

    def test_shell_text_in_model_stays_manifest_data(self):
        value = "$(touch never-run); `id`"
        _, files = candidate_files(self.root, form_body(overrides={"Model": value}))
        self.assertEqual(yaml.safe_load(next(iter(files.values())))["spec"]["model"], value)
        self.assertFalse((self.root / "never-run").exists())

    def test_unchecked_confirmations_rejected(self):
        with self.assertRaises(Error):
            decode(form_body().replace("- [X]", "- [ ]", 1))

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(Error):
            decode(form_body(overrides={"Provider options JSON": '{"max_tokens":1,"max_tokens":2}'}))

    def test_unapproved_upstream_and_provider_rejected(self):
        for body in (form_body("MCP", overrides={"Development upstream URL": "https://evil.corp/mcp"}),
                     form_body(overrides={"Provider": "anthropic"}),
                     form_body(overrides={"Provider options JSON": '{"upstream_url":"https://evil.corp"}'})):
            with self.assertRaises(Error):
                candidate_files(self.root, body)

    def test_external_sensitive_data_rejected(self):
        with self.assertRaises(Error):
            candidate_files(self.root, form_body(exposure="external", overrides={"Data classification": "confidential"}))

    def test_existing_resource_and_shared_upstream_cannot_be_overwritten(self):
        with self.assertRaises(Error):
            candidate_files(self.root, form_body(overrides={"Resource name": "support-chat"}))
        with self.assertRaises(Error):
            candidate_files(self.root, form_body("MCP", overrides={"Resource name": "ticketing-mcp"}))

    def test_conversion_mode_requires_safe_tool_definitions(self):
        with self.assertRaises(Error):
            candidate_files(self.root, form_body("MCP", overrides={"MCP mode": "conversion-listener"}))
        options = {"MCP mode": "conversion-listener", "Conversion tools JSON": json.dumps([
            {"name": "status", "description": "Read status", "method": "GET", "path": "/status"}])}
        candidate_files(self.root, form_body("MCP", overrides=options))
        options["Conversion tools JSON"] = options["Conversion tools JSON"].replace("/status", "//metadata.google.internal/")
        with self.assertRaises(Error):
            candidate_files(self.root, form_body("MCP", overrides=options))

    def test_segregation_and_us_region(self):
        resources, envs = load_source(self.root)
        self.assertEqual(len(envs), 24)
        self.assertEqual({e["region"] for e in envs.values()}, {"us"})
        self.assertEqual(enabled_lanes(resources, envs), [])
        for name, env in envs.items():
            doc = render(resources, env)
            actual = [k for k in doc if not k.startswith("_")]
            self.assertEqual(bool(actual), name.startswith("aws-internal-"))
        out = self.root / "bundle"
        build(self.root, out, "local")
        self.assertEqual(len(list(out.glob("*/environment.json"))), 24)

    def test_gateway_reuse_and_unconfigured_enabled_target_rejected(self):
        for mode in ("gateway", "partial"):
            with self.subTest(mode=mode):
                a = self.root / "environments/aws-internal-dev.yaml"
                b = self.root / "environments/azure-external-dev.yaml"
                originals = [a.read_text(), b.read_text()]
                ea, eb = read_yaml(a), read_yaml(b)
                if mode == "gateway":
                    ea["gateway_id"] = eb["gateway_id"] = "11111111-1111-1111-1111-111111111111"
                else:
                    ea["enabled"] = True
                a.write_text(yaml.safe_dump(ea)); b.write_text(yaml.safe_dump(eb))
                with self.assertRaises(Error):
                    load_source(self.root)
                a.write_text(originals[0]); b.write_text(originals[1])

    def test_metadata_local_and_non_https_upstreams_rejected(self):
        _, envs = load_source(self.root)
        env = envs["aws-internal-dev"]
        for url in ("http://development.approved.corp", "https://169.254.169.254", "https://metadata.google.internal",
                    "https://localhost", "https://development.approved.corp:8443", "https://user:pass@development.approved.corp"):
            with self.subTest(url=url), self.assertRaises(Error):
                approved_url(url, env)

    def test_enabled_target_requires_security_control_evidence(self):
        _, envs = load_source(self.root)
        env = copy.deepcopy(envs["aws-internal-dev"])
        env.update(enabled=True, cloud_scope="123456789012")
        with self.assertRaises(Error):
            live_controls(env)
        env["controls"] = {k: True for k in env["controls"]}
        with self.assertRaises(Error):
            live_controls(env)
        env["security_review_url"] = "https://security.corp/reviews/approved"
        live_controls(env)

    def api(self, existing=None, scan_failure=False):
        api = Mock()
        def respond(method, path, data=None, **kw):
            if path.startswith("/issues/"): return {"state": "open", "body": form_body()}
            if path == "/branches/main": return {"commit": {"sha": "a" * 40}}
            if path.startswith("/pulls?"): return existing or []
            if path.startswith("/git/ref/heads/"): return None
            if path == "/git/commits/" + "a" * 40: return {"tree": {"sha": "base-tree"}}
            if path == "/pulls": return {"number": 99}
            return {"sha": "b" * 40}
        api.request.side_effect = respond
        return api

    @patch.dict(os.environ, {"INTAKE_BASE_SHA": "a" * 40})
    def test_intake_creates_draft_from_trusted_base_with_limited_files(self):
        api, scanner = self.api(), Mock()
        result = intake.create_request(api, "example/repo", 7, self.root, scanner)
        self.assertEqual(result, {"number": 99, "status": "draft-created"})
        scanner.assert_called_once()
        calls = [c.args for c in api.request.call_args_list]
        tree = next(c[2] for c in calls if c[:2] == ("POST", "/git/trees"))
        self.assertEqual([x["path"] for x in tree["tree"]], ["catalog/llm/new-resource.yaml"])
        pr = next(c[2] for c in calls if c[:2] == ("POST", "/pulls"))
        self.assertTrue(pr["draft"])
        self.assertEqual(pr["base"], "main")

    @patch.dict(os.environ, {"INTAKE_BASE_SHA": "a" * 40})
    def test_existing_pr_is_not_modified(self):
        api = self.api(existing=[{"number": 99}])
        result = intake.create_request(api, "example/repo", 7, self.root, Mock())
        self.assertEqual(result["status"], "existing-request-unchanged")
        self.assertTrue(all(c.args[0] == "GET" for c in api.request.call_args_list))

    @patch.dict(os.environ, {"INTAKE_BASE_SHA": "a" * 40})
    def test_secret_scan_failure_prevents_all_mutations(self):
        api = self.api()
        with self.assertRaises(Error):
            intake.create_request(api, "example/repo", 7, self.root, Mock(side_effect=Error("Secret scan failed")))
        self.assertTrue(all(c.args[0] == "GET" for c in api.request.call_args_list))

    @patch.dict(os.environ, {"INTAKE_BASE_SHA": "c" * 40})
    def test_moving_main_cannot_mix_base_tree_and_candidate(self):
        api = self.api()
        with self.assertRaisesRegex(Error, "Main changed"):
            intake.create_request(api, "example/repo", 7, self.root, Mock())
        self.assertTrue(all(c.args[0] == "GET" for c in api.request.call_args_list))
