"""Refresh official release hashes for reviewed, fully pinned version inventories."""
import argparse
import json
import re
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([A-Za-z0-9.]+)")


def inventory(path):
    lines = [line.strip().rstrip(" \\") for line in path.read_text().splitlines()
             if line and not line.startswith((" ", "#"))]
    if not lines or not all(PIN.fullmatch(line) for line in lines) or len(lines) != len(set(lines)):
        raise SystemExit("Version inventories must contain unique exact package==version pins")
    return lines


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    for stem in ("requirements", "requirements-security"):
        pins = inventory(ROOT / (stem + ".txt"))
        output = ROOT / (stem + ".lock")
        if args.check:
            if set(pins) != set(inventory(output)):
                raise SystemExit(f"{stem}.txt differs from its hash lock; review versions and refresh")
            continue
        lines = ["# Official PyPI release hashes for reviewed, fully pinned dependencies."]
        for pin in pins:
            package, version = PIN.fullmatch(pin).groups()
            url = f"https://pypi.org/pypi/{package}/{version}/json"
            # Fixed HTTPS origin and strictly constrained package/version path segments.
            with urlopen(url, timeout=30) as response:  # nosec B310
                data = json.load(response)
            hashes = sorted({item["digests"]["sha256"] for item in data["urls"]})
            if not hashes or not all(re.fullmatch(r"[a-f0-9]{64}", h) for h in hashes):
                raise SystemExit("Missing or invalid release hashes")
            lines.append(pin + " \\\n" + " \\\n".join("    --hash=sha256:" + h for h in hashes))
        output.write_text("\n".join(lines) + "\n")
    print("Dependency lock inventories checked" if args.check else "Locks refreshed; verify installation and run both audits")


if __name__ == "__main__":
    main()
