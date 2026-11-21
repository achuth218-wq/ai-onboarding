import argparse
import json
import os
import sys
from pathlib import Path

from .build import build, load_source, validate_native, verify_bundle
from .adapters import render
from .common import Error
from .deploy import apply_plan, review_plan
from .policy import TARGETS, enabled_lanes, enabled_targets

def main():
    parser = argparse.ArgumentParser(description="Konnect AI Gateway onboarding")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("validate")
    p.add_argument("--source", default=".")
    p = commands.add_parser("targets")
    p.add_argument("--source", default=".")
    p = commands.add_parser("build")
    p.add_argument("--source", default=".")
    p.add_argument("--output", default="build/release")
    p.add_argument("--revision", default="local")
    p = commands.add_parser("verify")
    p.add_argument("--bundle", default="build/release")
    for verb in ("plan", "apply", "drift", "smoke"):
        p = commands.add_parser(verb)
        p.add_argument("--bundle", default="build/release")
        p.add_argument("--environment", choices=TARGETS, required=True)
        if verb != "smoke":
            p.add_argument("--output", default="build/receipt.json")
        if verb == "apply":
            p.add_argument("--expected-plan-digest", required=True)
    args = parser.parse_args()
    if args.command == "validate":
        resources, environments = load_source(args.source)
        for env in environments.values():
            validate_native(render(resources, env))
        print(f"Validated {len(resources)} resources across {len(environments)} environments")
    elif args.command == "targets":
        resources, environments = load_source(args.source)
        lanes = enabled_lanes(resources, environments)
        print(json.dumps(lanes))
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write("lanes=" + json.dumps(lanes) + "\n")
                f.write("has_targets=" + str(bool(lanes)).lower() + "\n")
                f.write("targets=" + json.dumps(enabled_targets(resources, environments)) + "\n")
    elif args.command == "build":
        print(build(args.source, args.output, args.revision))
    elif args.command == "verify":
        print(verify_bundle(args.bundle))
    elif args.command in ("plan", "drift"):
        receipt = review_plan(args.bundle, args.environment, args.output)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write("plan_digest=" + receipt["plan_digest"] + "\n")
        print(f"Plan: {receipt['plan_digest']}; {len(receipt['changes'])} changes")
        if args.command == "drift" and receipt["changes"]:
            return 2
    elif args.command == "apply":
        receipt = apply_plan(args.bundle, args.environment, args.expected_plan_digest, args.output)
        print("Deployment " + receipt["status"])
    elif args.command == "smoke":
        from .smoke import smoke
        verify_bundle(args.bundle)
        print(json.dumps(smoke(args.bundle, args.environment), indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Error, ValueError, KeyError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
