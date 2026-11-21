"""Read provenance, validate downloaded evidence, or record verified promotion."""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiops.common import Error, write_json
from aiops.promotion import GitHubRead, discover, promotion_receipt, validate_downloads


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["discover", "verify", "receipt"])
    args = parser.parse_args()
    context_path = Path("build/context/context.json")
    if args.command == "discover":
        repo = os.environ["GITHUB_REPOSITORY"]
        context = discover(GitHubRead(repo, os.environ.get("GH_TOKEN", "")), repo,
                           os.environ["RELEASE_RUN_ID"], os.environ["PROMOTION_LANE"], os.environ["PROMOTION_STAGE"],
                           os.environ.get("PREVIOUS_RUN_ID", ""), os.environ.get("PROMOTION_MODE", "promotion"))
        write_json(context_path, context)
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            for key in ("source_artifact_id", "previous_artifact_id"):
                f.write(key + "=" + str(context[key] or "") + "\n")
    elif args.command == "verify":
        context = json.loads(context_path.read_text())
        context = validate_downloads(context, "build/release", "build/prior/promotion.json", Path.cwd())
        write_json(context_path, context)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
                f.write(f"Promotion evidence verified for `{context['environment']}`.\n\n"
                        f"Release run: `{context['release_run_id']}`; bundle: `{context['bundle_digest']}`.\n\n"
                        "This run deploys only this target and does not trigger the next stage.\n")
    else:
        context = json.loads(context_path.read_text())
        deployment = json.loads(Path("build/deployment/deployment.json").read_text())
        receipt = promotion_receipt(context, deployment, os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"])
        write_json("build/promotion/promotion.json", receipt)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
                f.write(f"Verified `{context['environment']}`. Promotion run ID: `{receipt['promotion_run_id']}`.\n\n"
                        "Record this run ID for the next independently triggered stage.\n")


if __name__ == "__main__":
    try:
        main()
    except (Error, ValueError, KeyError, OSError) as exc:
        print(str(exc) if isinstance(exc, Error) else "Promotion evidence/configuration is incomplete", file=sys.stderr)
        sys.exit(1)
