#!/usr/bin/env python3
"""Export/check committed canonical source; never copy an arbitrary worktree."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tomllib

ROOT = Path(__file__).resolve().parents[2]
PREFIX = 'security_shell/'


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args])


def committed(commit):
    resolved = git('rev-parse', '--verify', commit + '^{commit}').decode().strip()
    data = git('archive', '--format=tar', resolved, 'security_shell')
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for item in archive:
            if item.isdir():
                continue
            path = Path(item.name)
            if not item.isfile() or path.is_absolute() or '..' in path.parts or not item.name.startswith(PREFIX):
                raise SystemExit('unsupported source entry')
            name = item.name[len(PREFIX):]
            if any(part in ('target', '__pycache__', '.venv', '.build-tools', '.pytest_cache') for part in path.parts) or path.suffix in ('.pem', '.key', '.whl', '.pyc') or 'credentials.json' in name:
                raise SystemExit('forbidden source artifact')
            files[name] = (archive.extractfile(item).read(), 0o755 if item.mode & 0o111 else 0o644)
    if 'Cargo.lock' not in files or 'pyproject.toml' not in files:
        raise SystemExit('canonical package is not committed')
    digest = hashlib.sha256()
    for name, (content, mode) in sorted(files.items()):
        digest.update(name.encode() + b'\0' + str(mode).encode() + b'\0' + hashlib.sha256(content).digest())
    provenance = {'schema_version': 1, 'wallet_commit': resolved, 'source_directory': 'security_shell',
                  'source_sha256': digest.hexdigest(), 'package_version': tomllib.loads(files['pyproject.toml'][0].decode())['project']['version'], 'protocol_version': 1}
    return files, provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['export', 'check'])
    parser.add_argument('--commit', required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    files, provenance = committed(args.commit)
    destination = args.destination
    if args.action == 'export':
        destination.mkdir(parents=True, exist_ok=False)
        for name, (content, mode) in files.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(mode)
        (destination / 'PROVENANCE.json').write_text(json.dumps(provenance, indent=2) + '\n')
    else:
        if destination.is_symlink():
            raise SystemExit('symlink snapshot root rejected')
        expected = set(files) | {'PROVENANCE.json'}
        actual = {p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file() or p.is_symlink()}
        if actual != expected or any(p.is_symlink() for p in destination.rglob('*')):
            raise SystemExit('source snapshot inventory mismatch')
        if json.loads((destination / 'PROVENANCE.json').read_text()) != provenance:
            raise SystemExit('provenance mismatch')
        for name, (content, mode) in files.items():
            target = destination / name
            if target.read_bytes() != content or target.stat().st_mode & 0o777 != mode:
                raise SystemExit('source snapshot mismatch')
    print(json.dumps(provenance, sort_keys=True))


if __name__ == '__main__':
    main()
