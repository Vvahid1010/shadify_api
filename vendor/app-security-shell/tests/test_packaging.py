"""Verify the reusable shell does not own the application's Redis client."""
import importlib.metadata
from pathlib import Path
import subprocess
import sys

import app_security_shell


def test_wheel_does_not_require_redis():
    distribution=importlib.metadata.distribution("app-security-shell")
    assert distribution.version == "0.4.6"
    assert distribution.requires == ["httpx==0.28.1", "anyio>=4.11,<5"]


def test_replay_import_does_not_import_redis():
    root=Path(app_security_shell.__file__).resolve().parent.parent
    code = """import importlib.abc, sys
class DenyRedis(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'redis' or fullname.startswith('redis.'):
            raise AssertionError('shell imported redis')
sys.meta_path.insert(0,DenyRedis())
sys.path.insert(0,sys.argv[1])
from app_security_shell.replay import RedisReplayStore
assert 'redis' not in sys.modules
"""
    subprocess.run([sys.executable,"-I","-c",code,str(root)],check=True,timeout=15)


def test_export_version_comes_from_committed_snapshot(monkeypatch):
    import importlib.util
    import io
    import tarfile
    spec=importlib.util.spec_from_file_location('shell_export_test',Path(__file__).resolve().parents[1]/'tools/export.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    for version in ('0.1.0','0.1.1'):
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w') as archive:
            for name,raw in {'Cargo.lock':b'# fixture', 'pyproject.toml':f'[project]\nversion="{version}"\n'.encode()}.items():
                info=tarfile.TarInfo('security_shell/'+name);info.size=len(raw);info.mode=0o644
                archive.addfile(info,io.BytesIO(raw))
        def git(*args):return ('a'*40).encode() if args[0]=='rev-parse' else stream.getvalue()
        monkeypatch.setattr(module,'git',git)
        _,provenance=module.committed('fixture')
        assert provenance['package_version']==version


def test_package_and_management_versions_agree():
    import json
    import tomllib
    root=Path(__file__).resolve().parents[1]
    python=tomllib.loads((root/'pyproject.toml').read_text())['project']['version']
    rust=tomllib.loads((root/'Cargo.toml').read_text())['workspace']['package']['version']
    # A reusable package must not assume an enclosing app checkout.
    assert python == rust == importlib.metadata.version('app-security-shell')
