import copy
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiops.adapters import render
from aiops.build import ROOT, build, deployment_inputs, load_source, validate_native, verify_bundle
from aiops.common import Error, digest, read_yaml
from aiops.deploy import apply_plan, assert_plan, check_nodes, normalize_plan


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        for directory in ("catalog", "environments"):
            shutil.copytree(ROOT / directory, self.source / directory)
        self.bundle = self.root / "bundle"
        self.resources, self.environments = load_source(self.source)
        self.env = self.environments["aws-internal-dev"]
        self.secrets = patch.dict(os.environ, {"AIOPS_LLM_AUTH_HEADER": "test-provider-key", "SMOKE_AUTH_VALUE": "test-consumer-key"})
        self.secrets.start()

    def tearDown(self):
        self.secrets.stop()
        self.temp.cleanup()

    def test_all_protocols_match_pinned_kong_schemas(self):
        for env in self.environments.values():
            validate_native(render(self.resources, env))

    def test_bundle_is_reproducible_and_detects_tampering(self):
        first = build(self.source, self.bundle, "a" * 40)
        second = build(self.source, self.root / "other", "a" * 40)
        self.assertEqual(first, second)
        self.assertEqual(first, verify_bundle(self.bundle))
        (self.bundle / "aws-internal-dev/kong.yaml").write_text("tampered")
        with self.assertRaisesRegex(Error, "Bundle changed"):
            verify_bundle(self.bundle)

    def test_extra_artifact_file_is_rejected(self):
        build(self.source, self.bundle, "local")
        (self.bundle / "unreviewed.yaml").write_text("x")
        with self.assertRaisesRegex(Error, "file list"):
            verify_bundle(self.bundle)

    def test_secret_value_never_enters_bundle(self):
        with patch.dict(os.environ, {"AIOPS_LLM_AUTH_HEADER": "sensitive-token"}):
            build(self.source, self.bundle, "local")
        output = (self.bundle / "aws-internal-dev/kong.yaml").read_text()
        self.assertNotIn("sensitive-token", output)
        self.assertIn("!secret", output)
        self.assertIn("AIOPS_LLM_AUTH_HEADER", output)

    def test_examples_cannot_deploy(self):
        build(self.source, self.bundle, "local")
        with self.assertRaisesRegex(Error, "disabled"):
            deployment_inputs(self.bundle, "aws-internal-dev")

    def test_legacy_gateway_product_is_rejected(self):
        p = self.source / "environments/aws-internal-dev.yaml"
        p.write_text(p.read_text().replace("kong-ai-gateway", "kong-gateway"))
        with self.assertRaises(Error):
            load_source(self.source)

    def test_unknown_input_fields_rejected(self):
        p = self.source / "catalog/llm/support-chat.yaml"
        p.write_text(p.read_text() + "unreviewed: true\n")
        with self.assertRaises(Error):
            load_source(self.source)

    def test_duplicate_yaml_keys_rejected(self):
        p = self.root / "duplicate.yaml"
        p.write_text("foo: one\nfoo: two\n")
        with self.assertRaisesRegex(Error, "Duplicate"):
            read_yaml(p)

    def test_overlapping_paths_rejected(self):
        p = self.source / "catalog/a2a/support-agent.yaml"
        p.write_text(p.read_text().replace("/a2a/support", "/llm/support/agent"))
        with self.assertRaisesRegex(Error, "Overlapping"):
            load_source(self.source)

    def test_conversion_mcp_supported(self):
        resource = next(r for r in self.resources if r["kind"] == "MCP")
        resource["spec"]["mode"] = "conversion-listener"
        resource["spec"]["tools"] = [{"name": "status", "description": "Read service status", "method": "GET", "path": "/status"}]
        validate_native(render([resource], self.env))

    def test_native_schema_rejects_unknown_provider(self):
        resource = next(r for r in self.resources if r["kind"] == "LLM")
        resource["spec"]["provider"] = "unsupported-provider"
        with self.assertRaises(Error):
            validate_native(render([resource], self.env))

    def node(self):
        return {"version": "2.3.1", "last_ping": 1000, "config_version": "cfg-2", "errors": [], "compatibility_status": {"state": "compatible"}}

    def test_readiness_checks_version_heartbeat_and_configuration(self):
        check_nodes([self.node()], self.env, "cfg-2", now=1001)
        for field, value in [("version", "2.2.0"), ("last_ping", 1), ("config_version", "old"), ("errors", [{"error": "bad"}])]:
            node = {**self.node(), field: value}
            with self.subTest(field=field), self.assertRaises(Error):
                check_nodes([node], self.env, "cfg-2", now=1001)

    def test_missing_nodes_rejected(self):
        with self.assertRaises(Error):
            check_nodes([], self.env, now=1001)

    def plan(self):
        return {"metadata": {"mode": "apply", "generated_at": "first"}, "changes": [
            {"action": "CREATE", "resource_type": "ai_gateway_model", "resource_ref": "support-chat",
             "namespace": self.env["namespace"], "fields": {"name": "support-chat"}}]}

    def test_plan_digest_ignores_time_but_detects_changed_fields(self):
        a, b = self.plan(), self.plan()
        b["metadata"]["generated_at"] = "second"
        self.assertEqual(digest(normalize_plan(a)), digest(normalize_plan(b)))
        b["changes"][0]["fields"]["name"] = "other"
        self.assertNotEqual(digest(normalize_plan(a)), digest(normalize_plan(b)))

    def test_delete_and_cross_namespace_plans_rejected(self):
        for key, value in [("action", "DELETE"), ("namespace", "other-team")]:
            plan = self.plan()
            plan["changes"][0][key] = value
            with self.assertRaises(Error):
                assert_plan(plan, self.env["namespace"])

    def test_stale_approval_never_calls_apply(self):
        build(self.source, self.bundle, "local")
        with patch("aiops.deploy.deployment_inputs", return_value=self.env), \
             patch("aiops.deploy.preflight"), patch("aiops.deploy.make_plan", return_value=self.plan()), \
             patch("aiops.deploy.command") as command:
            with self.assertRaisesRegex(Error, "Plan changed"):
                apply_plan(self.bundle, "aws-internal-dev", "stale-digest", self.root / "receipt.json")
            command.assert_not_called()

    def test_missing_credential_fails_before_any_network_or_write(self):
        build(self.source, self.bundle, "local")
        with patch("aiops.deploy.deployment_inputs", return_value=self.env), \
             patch("aiops.deploy.preflight") as preflight, \
             patch.dict(os.environ, {"AIOPS_LLM_AUTH_HEADER": "", "AIOPS_SECRET_VALUES": "{}"}):
            with self.assertRaisesRegex(Error, "credential is missing"):
                apply_plan(self.bundle, "aws-internal-dev", "digest", self.root / "receipt.json")
            preflight.assert_not_called()

    def test_apply_then_readiness_then_protocol_verification(self):
        build(self.source, self.bundle, "local")
        plan = self.plan()
        with patch("aiops.deploy.deployment_inputs", return_value=self.env), \
             patch("aiops.deploy.preflight"), patch("aiops.deploy.make_plan", return_value=plan), \
             patch("aiops.deploy.command") as command, patch("aiops.deploy.wait_ready", return_value="cfg-2"), \
             patch("aiops.smoke.smoke", return_value=[{"status": "passed"}]):
            receipt = apply_plan(self.bundle, "aws-internal-dev", digest(normalize_plan(plan)), self.root / "receipt.json")
        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(command.call_args.args[0][0], "apply")

    def test_failed_smoke_records_partial_deployment(self):
        build(self.source, self.bundle, "local")
        plan = self.plan()
        output = self.root / "receipt.json"
        with patch("aiops.deploy.deployment_inputs", return_value=self.env), \
             patch("aiops.deploy.preflight"), patch("aiops.deploy.make_plan", return_value=plan), \
             patch("aiops.deploy.command"), patch("aiops.deploy.wait_ready", return_value="cfg-2"), \
             patch("aiops.smoke.smoke", side_effect=Error("probe failed")):
            with self.assertRaises(Error):
                apply_plan(self.bundle, "aws-internal-dev", digest(normalize_plan(plan)), output)
        self.assertEqual(json.loads(output.read_text())["status"], "applied-awaiting-verification")


if __name__ == "__main__":
    unittest.main()
