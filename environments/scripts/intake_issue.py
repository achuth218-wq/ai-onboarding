"""Trusted default-branch code turns an issue into a draft PR through Git Data API.

Never checks out an issue-created branch. Never overwrites an existing PR/branch.
No Konnect token, cloud credential or private runner is needed.
"""
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiops.common import Error, required
from aiops.issue_forms import candidate_files
from aiops.policy import lane
from aiops.smoke import NoRedirects


class GitHub:
    def __init__(self, repository, token):
        required(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository), "Invalid repository")
        required(token, "GH_TOKEN is required")
        self.base = "https://api.github.com/repos/" + repository
        self.token = token

    def request(self, method, path, data=None, allow_missing=False):
        required(path.startswith("/") and not path.startswith("//"), "Invalid API path")
        req = Request(self.base + path, method=method,
                      data=json.dumps(data).encode() if data is not None else None,
                      headers={"Authorization": "Bearer " + self.token,
                               "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                               "X-GitHub-Api-Version": "2022-11-28"})
        try:
            # Fixed api.github.com HTTPS origin; never forward a token through redirects.
            with build_opener(NoRedirects()).open(req, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            if allow_missing and exc.code == 404:
                return None
            raise Error(f"GitHub API rejected {method} request (HTTP {exc.code}); check repository Actions permissions") from None


def scan(changes):
    executable = Path(".tools/gitleaks").resolve()
    required(executable.is_file(), "Install the pinned Gitleaks scanner before intake")
    with tempfile.TemporaryDirectory() as tmp:
        for name, content in changes.items():
            path = Path(tmp) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        result = subprocess.run([str(executable), "dir", tmp, "--redact", "--no-banner"],
                                capture_output=True, timeout=120)
        required(result.returncode == 0, "Secret scan failed; remove sensitive data from the issue before retrying")


def create_request(api, repository, number, root, scan_fn=scan):
    required(isinstance(number, int) and number > 0, "Invalid issue number")
    issue = api.request("GET", f"/issues/{number}")
    required(issue.get("state") == "open" and "pull_request" not in issue, "Select an open issue")
    info = api.request("GET", "/branches/main")
    # The complete candidate is generated against the exact trusted checkout, not a moving API tip.
    base_sha = os.environ["INTAKE_BASE_SHA"]
    required(re.fullmatch(r"[a-f0-9]{40}", base_sha), "Missing trusted checkout SHA")
    required(info["commit"]["sha"] == base_sha, "Main changed during intake; rerun against its current commit")
    branch = f"onboarding/issue-{number}"
    query = urlencode({"state": "all", "head": repository.split('/')[0] + ":" + branch, "base": "main"})
    existing = api.request("GET", "/pulls?" + query)
    if existing:
        return {"number": existing[0]["number"], "status": "existing-request-unchanged"}
    required(api.request("GET", "/git/ref/heads/" + branch, allow_missing=True) is None,
             "An intake branch exists without a PR; a maintainer must inspect/remove it before retrying")
    resource, changes = candidate_files(root, issue.get("body"))
    scan_fn(changes)
    commit = api.request("GET", "/git/commits/" + base_sha)
    tree = []
    for path, content in changes.items():
        blob = api.request("POST", "/git/blobs", {"content": base64.b64encode(content.encode()).decode(), "encoding": "base64"})
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    tree_sha = api.request("POST", "/git/trees", {"base_tree": commit["tree"]["sha"], "tree": tree})["sha"]
    title = f"Onboard {resource['kind']} {resource['metadata']['name']} to {lane(resource['placement'])}"
    created = api.request("POST", "/git/commits", {"message": title + f"\n\nSource issue #{number}", "tree": tree_sha, "parents": [base_sha]})
    api.request("POST", "/git/refs", {"ref": "refs/heads/" + branch, "sha": created["sha"]})
    body = (f"Closes #{number}.\n\nGenerated from the onboarding form. Review the catalog and upstream mappings, "
            "then run all required validation/security checks. Approve workflows to run if GitHub requests it. "
            "Mark ready only after platform/security review. Deployment follows the normal protected environment gates. "
            "Issue edits do not overwrite this PR; make subsequent changes in the PR.")
    pr = api.request("POST", "/pulls", {"title": title, "body": body, "head": branch, "base": "main", "draft": True})
    return {"number": pr["number"], "status": "draft-created"}


def main():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    number = os.environ.get("ISSUE_NUMBER") or str(event.get("issue", {}).get("number", ""))
    required(re.fullmatch(r"[1-9][0-9]{0,9}", number), "A numeric issue number is required")
    repository = os.environ["GITHUB_REPOSITORY"]
    result = create_request(GitHub(repository, os.environ.get("GH_TOKEN", "")), repository, int(number), Path.cwd())
    print(f"Onboarding PR #{result['number']}: {result['status']}")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(f"Onboarding PR: https://github.com/{repository}/pull/{result['number']}\n\nStatus: {result['status']}\n")


if __name__ == "__main__":
    try:
        main()
    except (Error, ValueError, KeyError, OSError) as exc:
        # Errors from input decoding are deliberately value-free.
        print(str(exc) if isinstance(exc, Error) else "Intake failed; check workflow configuration", file=sys.stderr)
        sys.exit(1)
