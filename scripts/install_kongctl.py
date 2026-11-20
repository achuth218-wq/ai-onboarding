#!/usr/bin/env python3
"""Install the pinned official CLI into a project-local directory; verify SHA-256."""
import hashlib
import json
import os
import platform
import urllib.request
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
lock = json.loads((root / "toolchain.lock.json").read_text())
system = platform.system().lower()
machine = {"x86_64": "amd64", "AMD64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine())
filename = f"kongctl_{system}_{machine}.zip"
expected = lock["kongctl"]["sha256"][filename]
directory = (root / ".tools").resolve()
directory.mkdir(exist_ok=True)
archive = directory / filename
url = f"https://github.com/Kong/kongctl/releases/download/v{lock['kongctl']['version']}/{filename}"
with urllib.request.urlopen(url, timeout=60) as response:
    archive.write_bytes(response.read())
if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
    raise SystemExit("Kong CLI archive checksum mismatch")
binary = "kongctl.exe" if system == "windows" else "kongctl"
with zipfile.ZipFile(archive) as z:
    # Extract only the executable, not arbitrary archive paths.
    (directory / binary).write_bytes(z.read(binary))
(directory / binary).chmod(0o755)
archive.unlink()
if os.environ.get("GITHUB_PATH"):
    with open(os.environ["GITHUB_PATH"], "a") as f:
        f.write(str(directory) + "\n")
print(f"Installed kongctl {lock['kongctl']['version']} in {directory}")
