"""Build Shadify's security shell from the same Git revision as its native release."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tomllib


def build_shell(root, commit, temp, record):
    directory = "vendor/app-security-shell"
    raw = subprocess.check_output(["git", "archive", commit + ":" + directory], cwd=root)
    source = temp / "shell-source"
    digest = hashlib.sha256()
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for entry in sorted(archive.getmembers(), key=lambda item: item.name):
            if entry.isdir():
                continue
            path = Path(entry.name)
            if not entry.isfile() or path.is_absolute() or ".." in path.parts:
                raise ValueError("invalid_shell_source")
            content = archive.extractfile(entry).read()
            mode = 0o755 if entry.mode & 0o111 else 0o644
            target = source / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(mode)
            if entry.name != "PROVENANCE.json":
                digest.update(entry.name.encode() + b"\0" + str(mode).encode() + b"\0"
                              + hashlib.sha256(content).digest())
    version = tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"]
    if record != {
        "schema_version": 2, "owner_app": "shadify_api", "source_directory": directory,
        "source_sha256": digest.hexdigest(), "package_version": version, "protocol_version": 1,
    } or record != json.loads((source / "PROVENANCE.json").read_text()):
        raise ValueError("shadify_shell_source_mismatch")
    env = os.environ.copy()
    env.pop("CREDENTIALS_DIRECTORY", None)
    env.setdefault("CARGO_TARGET_DIR", str(temp / "cargo-target"))
    output = temp / "shell-wheel"
    subprocess.run([
        "maturin", "build", "--locked", "--release", "--compatibility", "manylinux_2_38",
        "--manifest-path", str(source / "python-native/Cargo.toml"),
        "--interpreter", sys.executable, "--out", str(output),
    ], cwd=source, env=env, check=True)
    wheel = output / f"app_security_shell-{version}-cp314-cp314-manylinux_2_38_x86_64.whl"
    if list(output.glob("*.whl")) != [wheel]:
        raise ValueError("shadify_shell_wheel_missing")
    return wheel
