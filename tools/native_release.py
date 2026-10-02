"""Package committed API source and supplied offline wheels; no runtime/cloud actions."""
import argparse
from email.parser import BytesParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from native_release_contract import ROOT_FILES, RUNTIME, canonical, sha, verify

ROOT=Path(__file__).resolve().parents[1]


def build(root,wheels,output,source):
    if not re.fullmatch(r"[a-f0-9]{40}",source):raise ValueError("invalid_source")
    actual=subprocess.check_output(["git","rev-parse","HEAD"],cwd=root,text=True).strip()
    dirty=subprocess.check_output(["git","status","--porcelain","--untracked-files=no"],cwd=root)
    if actual!=source or dirty:raise ValueError("source_not_clean_and_pinned")
    names=subprocess.check_output(["git","ls-files","-z"],cwd=root).decode().split("\0")
    dependency=json.loads((root/"app_security_dependency.json").read_text())
    with tempfile.TemporaryDirectory(prefix="shadify-native-build-") as temporary:
        dest=Path(temporary)
        for name in filter(None,names):
            path=Path(name)
            if name not in ROOT_FILES and not (path.parts[0]=="shadify_api" and path.suffix==".py"
                       or path.parts[0]=="migrations" and path.suffix==".sql"):continue
            src=root/name
            if src.is_symlink() or not src.is_file():raise ValueError("invalid_source_file")
            target=dest/"source"/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(src.read_bytes())
        (dest/"wheels").mkdir()
        seen={};lock=[]
        for wheel in sorted(wheels.glob("*.whl")):
            if wheel.is_symlink() or not wheel.is_file():raise ValueError("invalid_wheel")
            with zipfile.ZipFile(wheel) as archive:
                entries=[n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
                if len(entries)!=1:raise ValueError("invalid_wheel_metadata")
                meta=BytesParser().parsebytes(archive.read(entries[0]))
            name,version=meta["Name"],meta["Version"]
            if not name or not version or not re.fullmatch(r"[A-Za-z0-9_.-]+",name):raise ValueError("invalid_wheel_identity")
            normalized=re.sub(r"[-_.]+","-",name).lower()
            if normalized in seen:raise ValueError("duplicate_wheel")
            if normalized=="app-security-shell":
                expected=dependency["source_validation_wheel"]
                if version!=dependency["package_version"] or wheel.name!=expected["filename"] or sha(wheel.read_bytes())!=expected["sha256"]:
                    raise ValueError("canonical_shell_wheel_mismatch")
            seen[normalized]=version
            shutil.copyfile(wheel,dest/"wheels"/wheel.name)
            lock.append(f"{name}=={version} --hash=sha256:{sha(wheel.read_bytes())}")
        if not {"fastapi","uvicorn","boto3","psycopg","psycopg-binary","httpx","redis","app-security-shell"}<=seen.keys():
            raise ValueError("incomplete_runtime_wheels")
        for line in (root/"requirements.txt").read_text().splitlines():
            match=re.fullmatch(r"([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([A-Za-z0-9_.+!-]+)",line)
            if not match:raise ValueError("unrecognized_runtime_requirement")
            name,version=match.groups()
            if seen.get(re.sub(r"[-_.]+","-",name).lower())!=version:
                raise ValueError("runtime_wheel_version_mismatch")
        (dest/"requirements.lock").write_text("\n".join(lock)+"\n")
        manifest={"schema":1,"app_id":"shadify_api","source_commit":source,"runtime":RUNTIME,
                  "requirements_path":"source/requirements.txt","lock_path":"requirements.lock",
                  "migration_target":"002_user_profile_track_drafts","management_schema_version":1,
                  "files":{p.relative_to(dest).as_posix():{"size":p.stat().st_size,"sha256":sha(p.read_bytes())}
                           for p in sorted(dest.rglob("*")) if p.is_file()}}
        (dest/"build-info.json").write_bytes(canonical(manifest))
        verify(dest,source)
        output.parent.mkdir(parents=True,exist_ok=True)
        with output.open("xb") as stream,tarfile.open(fileobj=stream,mode="w:gz") as archive:
            for path in sorted(dest.rglob("*")):
                if not path.is_file():continue
                info=archive.gettarinfo(str(path),path.relative_to(dest).as_posix())
                info.uid=info.gid=info.mtime=0;info.uname=info.gname="";info.mode=0o644
                with path.open("rb") as data:archive.addfile(info,data)
        return {"source_commit":source,"archive_sha256":sha(output.read_bytes()),"build_info_sha256":sha(canonical(manifest))}


if __name__=="__main__":
    if sys.version_info[:2]!=(3,14):raise SystemExit("Native package uses the existing Python 3.14 runtime")
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",required=True);parser.add_argument("--wheels",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    print(canonical(build(ROOT,args.wheels,args.output,args.source)).decode(),end="")
