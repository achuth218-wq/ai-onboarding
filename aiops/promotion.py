"""Independent stage promotion backed by trusted GitHub run and artifact evidence."""
import json
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from .adapters import render
from .build import deployment_inputs, validate_native, validate_schema, verify_bundle
from .common import Error, digest, read_yaml, required
from .policy import LANES, STAGES, TARGETS
from .smoke import NoRedirects


def positive_id(value):
    required(re.fullmatch(r"[1-9][0-9]{0,19}", str(value)), "Expected a positive numeric run/artifact ID")
    return int(value)


class GitHubRead:
    def __init__(self, repository, token):
        required(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository), "Invalid repository")
        required(token, "GH_TOKEN is required")
        self.repository, self.token = repository, token

    def get(self, path):
        required(path.startswith("/actions/"), "Only the Actions read API is allowed")
        req = Request("https://api.github.com/repos/" + self.repository + path,
                      headers={"Authorization": "Bearer " + self.token,
                               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with build_opener(NoRedirects()).open(req, timeout=30) as response:
                data = response.read(8 * 1024 * 1024 + 1)
                required(len(data) <= 8 * 1024 * 1024, "GitHub API response exceeded limit")
                return json.loads(data)
        except HTTPError as exc:
            raise Error(f"GitHub provenance read failed (HTTP {exc.code})") from None


def validate_run(run, repository, run_id, workflow, events):
    required(run.get("id") == positive_id(run_id), "Workflow run ID mismatch")
    required(run.get("status") == "completed" and run.get("conclusion") == "success",
             "Source workflow must have completed successfully")
    required(run.get("head_branch") == "main" and run.get("path") == workflow,
             "Source must be the expected protected main workflow")
    required(run.get("event") in events, "Unexpected source event")
    required(run.get("repository", {}).get("full_name") == repository
             and run.get("head_repository", {}).get("full_name") == repository,
             "Source workflow belongs to another repository or fork")
    required(re.fullmatch(r"[a-f0-9]{40}", run.get("head_sha", "")), "Invalid source revision")
    positive_id(run.get("run_attempt"))
    return run


def find_artifact(api, run_id, name):
    run_id = positive_id(run_id)
    matches = []
    for page in range(1, 51):
        data = api.get(f"/actions/runs/{run_id}/artifacts?per_page=100&page={page}")
        entries = data.get("artifacts")
        required(isinstance(entries, list), "Malformed artifact listing")
        matches.extend(a for a in entries if a.get("name") == name)
        if len(entries) < 100:
            break
    else:
        raise Error("Artifact listing exceeds supported size")
    required(len(matches) == 1, "Expected exactly one source artifact; missing, duplicate or rerun evidence")
    artifact = matches[0]
    required(artifact.get("expired") is False, "Required source artifact has expired")
    required(artifact.get("workflow_run", {}).get("id") == run_id, "Artifact belongs to another run")
    positive_id(artifact.get("id"))
    return artifact


def discover(api, repository, release_run_id, lane, stage, previous_run_id="", mode="promotion"):
    required(lane in LANES and stage in STAGES, "Unknown lane or stage")
    required(mode in ("promotion", "recovery"), "Unknown execution mode")
    release_run_id = positive_id(release_run_id)
    release = validate_run(api.get(f"/actions/runs/{release_run_id}"), repository, release_run_id,
                           ".github/workflows/release.yml", {"push", "workflow_dispatch"})
    artifact = find_artifact(api, release_run_id, "ai-release")
    context = {"format": 1, "repository": repository, "mode": mode, "lane": lane, "stage": stage,
               "environment": lane + "-" + stage, "release_run_id": release_run_id,
               "release_run_attempt": release["run_attempt"], "source_revision": release["head_sha"],
               "source_artifact_id": artifact["id"], "previous_run_id": None,
               "previous_run_attempt": None, "previous_stage": None, "previous_artifact_id": None}
    prior_stage = stage if mode == "recovery" else (STAGES[STAGES.index(stage) - 1] if stage != "dev" else None)
    if prior_stage:
        previous_run_id = positive_id(previous_run_id)
        previous = validate_run(api.get(f"/actions/runs/{previous_run_id}"), repository, previous_run_id,
                                f".github/workflows/deploy-{prior_stage}.yml", {"workflow_dispatch"})
        prior_artifact = find_artifact(api, previous_run_id, f"promotion-{lane}-{prior_stage}")
        context.update(previous_run_id=previous_run_id, previous_run_attempt=previous["run_attempt"],
                       previous_stage=prior_stage, previous_artifact_id=prior_artifact["id"])
    else:
        required(not previous_run_id, "Dev does not accept preceding-stage evidence")
    return context


def validate_prior(receipt, context, bundle_digest):
    expected = {"format": 1, "repository": context["repository"], "mode": "promotion", "status": "verified",
                "lane": context["lane"], "stage": context["previous_stage"],
                "environment": context["lane"] + "-" + context["previous_stage"],
                "release_run_id": context["release_run_id"], "release_run_attempt": context["release_run_attempt"],
                "source_artifact_id": context["source_artifact_id"],
                "source_revision": context["source_revision"], "bundle_digest": bundle_digest,
                "promotion_run_id": context["previous_run_id"],
                "promotion_run_attempt": context["previous_run_attempt"]}
    required(all(receipt.get(k) == v for k, v in expected.items()),
             "Prior deployment evidence does not match the release, lane, stage, run attempt or bundle")
    required(re.fullmatch(r"[a-f0-9]{64}", receipt.get("deployment_receipt_digest", "")),
             "Prior receipt lacks deployment evidence")


def validate_downloads(context, bundle, prior, source):
    bundle_digest = verify_bundle(bundle)
    meta = json.loads((Path(bundle) / "bundle.json").read_text())
    required(all(f"{target}/environment.json" in meta["files"] and f"{target}/kong.yaml" in meta["files"]
                 for target in TARGETS), "Release must contain all 24 four-stage targets; build a new release")
    required(meta["source_revision"] == context["source_revision"], "Release artifact source revision mismatch")
    env = deployment_inputs(bundle, context["environment"])
    current = read_yaml(Path(source) / "environments" / (context["environment"] + ".yaml"))
    required(current == env, "Target configuration changed since release; build a new release and restart at dev")
    resources = json.loads((Path(bundle) / "catalog.json").read_text())
    for resource in resources:
        validate_schema(resource, "onboarding.json")
    validate_native(render(resources, env))
    if context["previous_stage"]:
        receipt = json.loads(Path(prior).read_text())
        validate_prior(receipt, context, bundle_digest)
    return {**context, "bundle_digest": bundle_digest}


def promotion_receipt(context, deployment, run_id, attempt):
    required(context.get("bundle_digest"), "Promotion context was not validated")
    required(deployment.get("status") == "verified" and deployment.get("environment") == context["environment"]
             and deployment.get("bundle_digest") == context["bundle_digest"],
             "Deployment receipt is not verified for the selected release and target")
    required(deployment.get("checks") and all(c.get("status") == "passed" for c in deployment["checks"]),
             "Deployment lacks successful protocol verification")
    return {**context, "status": "verified", "promotion_run_id": positive_id(run_id),
            "promotion_run_attempt": positive_id(attempt), "deployment_receipt_digest": digest(deployment)}
