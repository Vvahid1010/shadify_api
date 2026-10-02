"""The existing source/wheels/hash-lock/build-info native artifact format."""
import hashlib
import json
import re
import stat
from pathlib import PurePosixPath

RUNTIME = {"kind":"python", "python_minor":"3.14", "platform":"linux", "architecture":"x86_64"}
ROOT_FILES = {"requirements.txt", "pyproject.toml", "management_schema.json", "config_overlays.json", "app_security_dependency.json", "node_agent_transport_dependency.json"}
REQUIRED = {"source/" + name for name in ROOT_FILES - {"node_agent_transport_dependency.json"}} | {"source/shadify_api/main.py", "source/shadify_api/runtime.py",
    "source/migrations/001_media_foundation.sql", "source/migrations/002_user_profile_track_drafts.sql", "requirements.lock"}


def canonical(value):
    return (json.dumps(value,sort_keys=True,separators=(",", ":"))+"\n").encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def allowed(name):
    path=PurePosixPath(name)
    if (not name or path.is_absolute() or path.as_posix()!=name or "\\" in name
            or any(part in {".",".."} or part.startswith(".") for part in path.parts)):
        return False
    return (name == "requirements.lock" or len(path.parts)==2 and path.parts[0]=="wheels" and path.suffix==".whl"
        or len(path.parts)==2 and path.parts[0]=="source" and path.name in ROOT_FILES
        or len(path.parts)>2 and path.parts[:2]==("source","shadify_api") and path.suffix==".py"
        or len(path.parts)==3 and path.parts[:2]==("source","migrations") and path.suffix==".sql")


def verify(root, source):
    manifest=root/"build-info.json"
    if not manifest.is_file() or manifest.is_symlink():raise ValueError("invalid_manifest_file")
    raw=manifest.read_bytes()
    data=json.loads(raw)
    if (not isinstance(data,dict) or set(data)!={"schema","app_id","source_commit","runtime","requirements_path",
            "lock_path","migration_target","management_schema_version","files"}
        or raw!=canonical(data) or type(data.get("schema")) is not int or data.get("schema")!=1 or data.get("app_id")!="shadify_api"
        or data.get("source_commit")!=source or not re.fullmatch(r"[a-f0-9]{40}",source)
        or data.get("runtime")!=RUNTIME or data.get("requirements_path")!="source/requirements.txt"
        or data.get("lock_path")!="requirements.lock" or data.get("management_schema_version")!=1
        or data.get("migration_target")!="002_user_profile_track_drafts"):
        raise ValueError("invalid_manifest")
    files=data.get("files")
    if not isinstance(files,dict) or not REQUIRED<=files.keys():
        raise ValueError("incomplete_native_package")
    actual=set()
    for path in root.rglob("*"):
        info=path.lstat()
        if stat.S_ISDIR(info.st_mode):continue
        name=path.relative_to(root).as_posix()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise ValueError("invalid_file")
        if name=="build-info.json":continue
        if not allowed(name) or files.get(name)!={"size":info.st_size,"sha256":sha(path.read_bytes())}:
            raise ValueError("invalid_package_file")
        actual.add(name)
    if actual!=files.keys():raise ValueError("manifest_inventory_mismatch")
    if "node-agent-local-shell-transport==" in (root/"source/requirements.txt").read_text():
        if "source/node_agent_transport_dependency.json" not in files:
            raise ValueError("missing_transport_provenance")
    return data
