"""Install pinned, SHA-256 verified scanner/linter executables only."""
import hashlib
import io
import json
import platform
import tarfile
from pathlib import Path
from urllib.request import urlopen


def main():
    root = Path(__file__).resolve().parents[1]
    lock = json.loads((root / "toolchain.lock.json").read_text())
    system = platform.system().lower()
    arch = {"x86_64": "amd64", "AMD64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    if system not in ("linux", "darwin") or not arch:
        raise RuntimeError("Use Linux/macOS x64 or arm64 for the security tools")
    dest = root / ".tools"
    dest.mkdir(exist_ok=True)
    for tool, repo in (("gitleaks", "gitleaks/gitleaks"), ("actionlint", "rhysd/actionlint")):
        version = lock[tool]["version"]
        machine = "x64" if tool == "gitleaks" and arch == "amd64" else arch
        name = f"{tool}_{version}_{system}_{machine}.tar.gz"
        expected = lock[tool]["sha256"][name]
        url = f"https://github.com/{repo}/releases/download/v{version}/{name}"
        # Fixed HTTPS release host and a committed checksum; no user-supplied URL.
        with urlopen(url, timeout=60) as response:  # nosec B310
            data = response.read(100 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError("Security-tool archive checksum mismatch")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            member = archive.getmember(tool)
            if not member.isfile() or member.size > 100 * 1024 * 1024:
                raise RuntimeError("Unexpected executable in scanner archive")
            with archive.extractfile(member) as stream:
                (dest / tool).write_bytes(stream.read())
        (dest / tool).chmod(0o755)
        print(f"Verified {tool} {version}")


if __name__ == "__main__":
    main()
