"""Repository workflow policy. Run in read-only PR CI and again before release."""
import re
from pathlib import Path

import yaml


def check(root):
    errors = []
    for path in sorted((root / ".github/workflows").glob("*.yml")):
        # BaseLoader only constructs strings/lists/dicts, never Python objects.
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)  # nosec B506
        events = workflow.get("on", {})
        if "pull_request_target" in events:
            errors.append(f"{path.name}: privileged PR target event is forbidden")
        permissions = workflow.get("permissions", {})
        if permissions.get("contents") != "read" or set(permissions) - {"contents", "actions"} or any(v != "read" for v in permissions.values()):
            errors.append(f"{path.name}: default permits only contents: read and optional actions: read")
        for name, job in workflow.get("jobs", {}).items():
            if ("pull_request" in events or "issues" in events) and "self-hosted" in str(job.get("runs-on", "")):
                errors.append(f"{path.name}/{name}: untrusted events must not use private runners")
            elevated = job.get("permissions", {})
            if "write" in elevated.values() and not (path.name == "intake.yml" and name == "intake"
                    and elevated == {"contents": "write", "pull-requests": "write", "issues": "read"}):
                errors.append(f"{path.name}/{name}: unexpected elevated token permissions")
            if job.get("secrets") == "inherit":
                errors.append(f"{path.name}/{name}: secret inheritance is forbidden")
            for step in job.get("steps", []):
                action = step.get("uses", "")
                if action and not action.startswith("./") and not re.fullmatch(r"[\w./-]+@[a-f0-9]{40}", action):
                    errors.append(f"{path.name}/{name}: action must be pinned to a full SHA")
                if action.startswith("actions/checkout@") and step.get("with", {}).get("persist-credentials") != "false":
                    errors.append(f"{path.name}/{name}: checkout must not persist credentials")
                if "${{" in step.get("run", ""):
                    errors.append(f"{path.name}/{name}: expressions cannot be interpolated into scripts; pass environment data")
    if errors:
        raise SystemExit("\n".join(errors))
    print("Workflow security policy passed")


if __name__ == "__main__":
    check(Path(__file__).resolve().parents[1])
