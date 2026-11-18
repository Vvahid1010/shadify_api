#!/usr/bin/env python3
"""Record local wheel integrity and committed source provenance."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import zipfile

from export import ROOT, committed, git


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if git('status','--porcelain','--untracked-files=all','--','security_shell').strip():
        raise SystemExit('commit canonical package before recording artifact provenance')
    _,source=committed('HEAD')
    with zipfile.ZipFile(args.wheel) as wheel:
        metadata=next(n for n in wheel.namelist() if n.endswith('.dist-info/WHEEL'))
        tags=[line[5:] for line in wheel.read(metadata).decode().splitlines() if line.startswith('Tag: ')]
    def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
    manifest={'schema_version':1,'source':source,'wheel':args.wheel.name,'wheel_sha256':digest(args.wheel),
              'wheel_tags':tags,'python':platform.python_version(),'machine':platform.machine(),
              'rustc':subprocess.check_output(['rustc','--version'],text=True).strip(),
              'locks':{name:digest(ROOT/'security_shell'/name) for name in ('Cargo.lock','requirements-wheel.lock')},
              'scope':'local_build_integrity_only_not_production_acceptance'}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps(manifest,sort_keys=True))


if __name__=='__main__': main()
