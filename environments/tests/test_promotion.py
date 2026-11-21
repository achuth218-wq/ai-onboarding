import copy
import json
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

import yaml

from aiops.build import ROOT, build, load_source
from aiops.common import Error, digest, read_yaml
from aiops.policy import STAGES, enabled_lanes, enabled_targets
from aiops.promotion import discover, find_artifact, promotion_receipt, validate_downloads, validate_prior, validate_run

REPO = "example/platform"
SHA = "a" * 40


def run(run_id=100, path=".github/workflows/release.yml", **kwargs):
    return {"id": run_id, "run_attempt": 1, "status": "completed", "conclusion": "success",
            "head_branch": "main", "head_sha": SHA, "event": "workflow_dispatch", "path": path,
            "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO}, **kwargs}


class API:
    def __init__(self, prior_stage="dev"):
        self.runs = {100: run(), 200: run(200, f".github/workflows/deploy-{prior_stage}.yml")}
        self.artifacts = {100: [{"id": 101, "name": "ai-release", "expired": False, "workflow_run": {"id": 100}}],
                          200: [{"id": 201, "name": f"promotion-aws-internal-{prior_stage}", "expired": False,
                                 "workflow_run": {"id": 200}}]}
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        run_id = int(path.split("/")[3])
        return {"artifacts": self.artifacts[run_id]} if "/artifacts?" in path else self.runs[run_id]


def prior_receipt(context, digest="b" * 64):
    return {"format": 1, "repository": REPO, "mode": "promotion", "status": "verified",
            "lane": "aws-internal", "stage": context["previous_stage"],
            "environment": "aws-internal-" + context["previous_stage"], "release_run_id": 100,
            "release_run_attempt": 1, "source_artifact_id": 101, "source_revision": SHA,
            "bundle_digest": digest, "promotion_run_id": 200, "promotion_run_attempt": 1,
            "deployment_receipt_digest": "d" * 64}


class ProvenanceTests(unittest.TestCase):
    def test_dev_uses_release_without_prior_stage(self):
        api = API()
        context = discover(api, REPO, "100", "aws-internal", "dev")
        self.assertEqual(context["source_artifact_id"], 101)
        self.assertIsNone(context["previous_run_id"])
        self.assertFalse(any("/200" in call for call in api.calls))

    def test_each_promotion_checks_exact_predecessor(self):
        for current, previous in [("test", "dev"), ("preprod", "test"), ("prod", "preprod")]:
            with self.subTest(stage=current):
                context = discover(API(previous), REPO, 100, "aws-internal", current, 200)
                self.assertEqual(context["previous_stage"], previous)
                validate_prior(prior_receipt(context), context, "b" * 64)

    def test_missing_previous_run_and_invalid_selection_rejected(self):
        for lane, stage, previous in [("aws-internal", "test", ""), ("aws-internal", "dev", "200"),
                                      ("../../secret", "dev", ""), ("aws-internal", "staging", "")]:
            with self.subTest(stage=stage), self.assertRaises(Error):
                discover(API(), REPO, 100, lane, stage, previous)

    def test_preprod_cannot_skip_test(self):
        with self.assertRaisesRegex(Error, "expected protected main workflow"):
            discover(API("dev"), REPO, 100, "aws-internal", "preprod", 200)

    def test_failed_fork_branch_event_and_wrong_workflow_rejected(self):
        mutations = [("conclusion", "failure"), ("status", "in_progress"), ("head_branch", "feature"),
                     ("path", ".github/workflows/validate.yml"), ("event", "pull_request"),
                     ("repository", {"full_name": "attacker/fork"}), ("head_repository", {"full_name": "attacker/fork"}),
                     ("head_sha", "main"), ("id", 999), ("run_attempt", 0)]
        for key, value in mutations:
            with self.subTest(field=key), self.assertRaises(Error):
                validate_run(run(**{key: value}), REPO, 100, ".github/workflows/release.yml", {"push", "workflow_dispatch"})

    def test_missing_duplicate_expired_and_foreign_artifacts_rejected(self):
        for mode in ("missing", "duplicate", "expired", "foreign"):
            api = API()
            if mode == "missing": api.artifacts[100] = []
            if mode == "duplicate": api.artifacts[100] *= 2
            if mode == "expired": api.artifacts[100][0]["expired"] = True
            if mode == "foreign": api.artifacts[100][0]["workflow_run"]["id"] = 500
            with self.subTest(mode=mode), self.assertRaises(Error):
                find_artifact(api, 100, "ai-release")

    def test_receipt_binds_release_lane_stage_digest_and_attempt(self):
        context = discover(API(), REPO, 100, "aws-internal", "test", 200)
        mutations = [("release_run_id", 999), ("source_artifact_id", 999), ("source_revision", "c" * 40),
                     ("bundle_digest", "e" * 64), ("lane", "aws-external"), ("stage", "test"),
                     ("environment", "azure-internal-dev"), ("repository", "attacker/fork"),
                     ("promotion_run_id", 999), ("promotion_run_attempt", 2), ("release_run_attempt", 2),
                     ("status", "applied-awaiting-verification"), ("mode", "recovery"),
                     ("deployment_receipt_digest", "")]
        for key, value in mutations:
            evidence = prior_receipt(context); evidence[key] = value
            with self.subTest(field=key), self.assertRaises(Error):
                validate_prior(evidence, context, "b" * 64)

    def test_recovery_requires_previous_success_at_the_same_target_stage(self):
        context = discover(API("prod"), REPO, 100, "aws-internal", "prod", 200, "recovery")
        self.assertEqual(context["previous_stage"], "prod")
        validate_prior(prior_receipt(context), context, "b" * 64)
        with self.assertRaises(Error):
            discover(API("preprod"), REPO, 100, "aws-internal", "prod", 200, "recovery")

    def test_no_receipt_on_partial_apply_or_missing_protocol_verification(self):
        context = {**discover(API(), REPO, 100, "aws-internal", "dev"), "bundle_digest": "b" * 64}
        deployed = {"status": "verified", "environment": "aws-internal-dev", "bundle_digest": "b" * 64,
                    "checks": [{"status": "passed"}]}
        self.assertEqual(promotion_receipt(context, deployed, 300, 1)["promotion_run_id"], 300)
        for key, value in [("status", "apply-started"), ("environment", "aws-external-dev"),
                           ("bundle_digest", "c" * 64), ("checks", []), ("checks", [{"status": "failed"}])]:
            with self.subTest(field=key), self.assertRaises(Error):
                promotion_receipt(context, {**deployed, key: value}, 300, 1)


class BundlePromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        for directory in ("catalog", "environments"):
            shutil.copytree(ROOT / directory, self.source / directory)
        for index, stage in enumerate(STAGES, 1):
            p = self.source / f"environments/aws-internal-{stage}.yaml"
            env = yaml.safe_load(p.read_text().replace(".example.invalid", ".approved.corp"))
            env.update(enabled=True, gateway_id=str(uuid.UUID(int=index)), auth_strategy_id=str(uuid.UUID(int=100)),
                       cloud_scope="123456789012", security_review_url="https://reviews.corp/approval")
            env["controls"] = {k: True for k in env["controls"]}
            env["policy_profiles"] = {k: ["approved-policy"] for k in env["policy_profiles"]}
            p.write_text(yaml.safe_dump(env, sort_keys=False))
        self.bundle = self.root / "bundle"
        self.bundle_digest = build(self.source, self.bundle, SHA)
        self.context = discover(API(), REPO, 100, "aws-internal", "test", 200)
        self.prior = self.root / "promotion.json"
        self.prior.write_text(json.dumps(prior_receipt(self.context, self.bundle_digest)))

    def tearDown(self):
        self.temp.cleanup()

    def test_original_bundle_accepted_without_rebuild(self):
        result = validate_downloads(self.context, self.bundle, self.prior, self.source)
        self.assertEqual(result["bundle_digest"], self.bundle_digest)

    def test_incomplete_legacy_target_inventory_rejected_even_with_valid_checksums(self):
        meta_path = self.bundle / "bundle.json"
        metadata = json.loads(meta_path.read_text())
        for relative in list(metadata["files"]):
            if "/" in relative and relative.split("/")[0].endswith("-preprod"):
                (self.bundle / relative).unlink()
                del metadata["files"][relative]
        metadata.pop("bundle_digest")
        metadata["bundle_digest"] = digest(metadata)
        meta_path.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(Error, "all 24"):
            validate_downloads(self.context, self.bundle, self.prior, self.source)

    def test_source_sha_mismatch_and_changed_current_target_rejected(self):
        with self.assertRaisesRegex(Error, "source revision"):
            validate_downloads({**self.context, "source_revision": "d" * 40}, self.bundle, self.prior, self.source)
        p = self.source / "environments/aws-internal-test.yaml"
        env = read_yaml(p); env["enabled"] = False; p.write_text(yaml.safe_dump(env))
        with self.assertRaisesRegex(Error, "Target configuration changed"):
            validate_downloads(self.context, self.bundle, self.prior, self.source)

    def test_one_stage_can_be_enabled_without_enabling_others(self):
        for stage in ("test", "preprod", "prod"):
            p = self.source / f"environments/aws-internal-{stage}.yaml"
            env = read_yaml(p); env["enabled"] = False; p.write_text(yaml.safe_dump(env))
        resources, environments = load_source(self.source)
        self.assertEqual(enabled_targets(resources, environments), ["aws-internal-dev"])
        self.assertEqual(enabled_lanes(resources, environments), ["aws-internal"])


class WorkflowTriggerTests(unittest.TestCase):
    def test_build_never_deploys_and_each_stage_has_an_independent_trigger(self):
        workflows = ROOT / ".github/workflows"
        release = read_yaml(workflows / "release.yml")
        self.assertEqual(set(release["jobs"]), {"security", "build"})
        for stage in STAGES:
            workflow = read_yaml(workflows / f"deploy-{stage}.yml")
            self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
            job = workflow["jobs"]["promote"]
            self.assertEqual(job["with"]["stage"], stage)
            self.assertEqual(job["with"]["mode"], "promotion")
            self.assertEqual(job["if"], "github.ref == 'refs/heads/main'")
            self.assertEqual("previous_run_id" in workflow["on"]["workflow_dispatch"]["inputs"], stage != "dev")
