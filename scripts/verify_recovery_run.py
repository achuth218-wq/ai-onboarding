#!/usr/bin/env python3
"""Only recover a successful release artifact produced by this repository."""
import json
import os
import re
import urllib.request

run_id = os.environ["SOURCE_RUN_ID"]
if not re.fullmatch(r"[0-9]+", run_id):
    raise SystemExit("Run ID must be numeric")
repo = os.environ["GITHUB_REPOSITORY"]
url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}"
request = urllib.request.Request(url, headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                                              "Accept": "application/vnd.github+json"})
with urllib.request.urlopen(request, timeout=30) as response:
    run = json.load(response)
if not (run["conclusion"] == "success" and run["head_branch"] == "main"
        and run["path"] == ".github/workflows/release.yml"
        and run["event"] in ("push", "workflow_dispatch")
        and run["repository"]["full_name"] == repo
        and run["head_repository"]["full_name"] == repo):
    raise SystemExit("Recovery source must be a successful main-branch Release workflow in this repository")
with open(os.environ["GITHUB_OUTPUT"], "a") as f:
    f.write("source_sha=" + run["head_sha"] + "\n")
