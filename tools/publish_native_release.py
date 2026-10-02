"""Publish the existing three-version build snapshot with an ordinary non-force push.

Uses Git's existing credential helper, never reads a token or application secrets.
The new build commit retains the old build commit as its parent.
"""
import argparse
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
from native_release_contract import allowed, canonical, sha, verify

ROOT=Path(__file__).resolve().parents[1]
REF="refs/heads/build"


def git(args,cwd,*,env=None,data=None):
    return subprocess.check_output(["git",*args],cwd=cwd,env=env,input=data)


def extract(archive,destination,prefix=""):
    members=[];seen=set();total=0
    for member in archive:
        name=member.name.rstrip("/")
        if prefix:
            if member.isdir() and name==prefix:continue
            if not name.startswith(prefix+"/"):raise ValueError("invalid_archive_prefix")
            name=name[len(prefix)+1:]
        parts=Path(name).parts
        if (not name or name.startswith("/") or "\\" in name or any(p in {"..","."} for p in parts)
                or Path(name).as_posix()!=name or name in seen or len(seen)>30000
                or not (member.isfile() or member.isdir())):raise ValueError("invalid_archive")
        seen.add(name);total+=member.size
        if total>512*1024*1024:raise ValueError("archive_too_large")
        members.append((member,name))
    destination.mkdir(parents=True,exist_ok=False)
    for member,name in members:
        target=destination/name
        if member.isdir():target.mkdir(parents=True,exist_ok=True);continue
        target.parent.mkdir(parents=True,exist_ok=True)
        with target.open("xb") as out,archive.extractfile(member) as source:
            shutil.copyfileobj(source,out)
        target.chmod(0o644)


def snapshot(incoming,previous,target,source):
    verify(incoming,source)
    versions=[]
    if previous is not None:
        raw=(previous/"latest.json").read_bytes();record=json.loads(raw)
        if raw!=canonical(record) or set(record)!={"schema","versions"} or record["schema"]!=1:
            raise ValueError("invalid_latest")
        versions=record["versions"]
        if not isinstance(versions,list) or not 1<=len(versions)<=3:raise ValueError("invalid_latest")
        for item in versions:
            if set(item)!={"source_commit","build_info_sha256"}:raise ValueError("invalid_latest")
            version=item["source_commit"]
            if not re.fullmatch(r"[a-f0-9]{40}",version):raise ValueError("invalid_latest")
            verify(previous/version,version)
            if sha((previous/version/"build-info.json").read_bytes())!=item["build_info_sha256"]:
                raise ValueError("invalid_previous_digest")
        if len({v["source_commit"] for v in versions})!=len(versions):raise ValueError("duplicate_version")
        if {p.name for p in previous.iterdir()}!={"latest.json",*(v["source_commit"] for v in versions)}:
            raise ValueError("unexpected_snapshot_content")
    if any(v["source_commit"]==source for v in versions):raise ValueError("source_already_published")
    target.mkdir()
    new={"source_commit":source,"build_info_sha256":sha((incoming/"build-info.json").read_bytes())}
    retained=[new,*versions[:2]]
    for item in retained:
        origin=incoming if item is new else previous/item["source_commit"]
        shutil.copytree(origin,target/item["source_commit"])
    (target/"latest.json").write_bytes(canonical({"schema":1,"versions":retained}))


def publish(source,archive):
    if not re.fullmatch(r"[a-f0-9]{40}",source):raise ValueError("invalid_source")
    remote=git(["remote","get-url","origin"],ROOT).decode().strip()
    if remote!="https://github.com/Vvahid1010/shadify_api.git":raise ValueError("unexpected_repository")
    line=git(["ls-remote",remote,"refs/heads/main"],ROOT).decode().strip()
    if line!=source+"\trefs/heads/main":raise ValueError("source_not_current_published_main")
    old_line=git(["ls-remote",remote,REF],ROOT).decode().strip()
    if old_line and not re.fullmatch(r"[a-f0-9]{40}\trefs/heads/build",old_line):raise ValueError("invalid_build_ref")
    old=old_line.split()[0] if old_line else ""
    with tempfile.TemporaryDirectory(prefix="shadify-publish-") as temporary:
        temp=Path(temporary);incoming=temp/"incoming"
        with tarfile.open(archive) as file:extract(file,incoming)
        verify(incoming,source)
        work=temp/"git";work.mkdir();git(["init","--quiet"],work)
        previous=None
        if old:
            git(["fetch","--no-tags",remote,REF],work)
            if git(["rev-parse","FETCH_HEAD"],work).decode().strip()!=old:raise ValueError("publication_race")
            raw=git(["archive",old,"release"],work);previous=temp/"previous"
            with tarfile.open(fileobj=io.BytesIO(raw)) as file:extract(file,previous,"release")
        snapshot(incoming,previous,work/"release",source)
        if previous is not None:
            latest=json.loads((previous/"latest.json").read_text())["versions"][0]["source_commit"]
            if subprocess.run(["git","merge-base","--is-ancestor",latest,source],cwd=ROOT,check=False).returncode:
                raise ValueError("stale_or_divergent_build")
        git(["add","--","release"],work)
        tree=git(["write-tree"],work).decode().strip()
        name=git(["config","user.name"],ROOT).decode().strip()
        email=git(["config","user.email"],ROOT).decode().strip()
        env={**os.environ,"GIT_AUTHOR_NAME":name,"GIT_COMMITTER_NAME":name,
             "GIT_AUTHOR_EMAIL":email,"GIT_COMMITTER_EMAIL":email}
        args=["commit-tree",tree]+(["-p",old] if old else [])
        commit=git(args,work,env=env,data=f"Build {source}\n".encode()).decode().strip()
        git(["push",remote,commit+":"+REF],work)
        if git(["ls-remote",remote,REF],work).decode().strip()!=commit+"\t"+REF:
            raise ValueError("remote_build_verification_failed")
        return {"source_commit":source,"build_commit":commit,
                "build_info_sha256":sha((incoming/"build-info.json").read_bytes())}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",required=True);parser.add_argument("--archive",type=Path,required=True)
    args=parser.parse_args()
    print(canonical(publish(args.source,args.archive)).decode(),end="")
