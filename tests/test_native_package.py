import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

TOOLS=Path(__file__).resolve().parents[1]/"tools"
sys.path.insert(0,str(TOOLS))
from native_release_contract import ROOT_FILES,RUNTIME,canonical,sha,verify
from publish_native_release import extract,snapshot


def package(directory,source):
    from native_release_contract import REQUIRED
    files={name:b"{}" for name in REQUIRED}
    for name,raw in files.items():
        path=directory/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    manifest={"schema":1,"app_id":"shadify_api","source_commit":source,"runtime":RUNTIME,
              "requirements_path":"source/requirements.txt","lock_path":"requirements.lock",
              "migration_target":"002_user_profile_track_drafts","management_schema_version":1,
              "files":{name:{"size":len(raw),"sha256":sha(raw)} for name,raw in files.items()}}
    (directory/"build-info.json").write_bytes(canonical(manifest))
    return manifest


class NativePackageTests(unittest.TestCase):
    def test_manifest_inventory_and_digest_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source="a"*40;manifest=package(root,source)
            self.assertEqual(verify(root,source),manifest)
            (root/"source/shadify_api/main.py").write_bytes(b"changed")
            with self.assertRaises(ValueError):verify(root,source)

    def test_credential_and_symlink_files_cannot_enter_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source="a"*40;package(root,source)
            secret=root/"source/storage.credentials.json";secret.write_bytes(b"synthetic")
            with self.assertRaises(ValueError):verify(root,source)
            secret.unlink();(root/"source/alias").symlink_to(root/"source/shadify_api/main.py")
            with self.assertRaises(ValueError):verify(root,source)

    def test_archive_traversal_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw=io.BytesIO()
            with tarfile.open(fileobj=raw,mode="w") as archive:
                item=tarfile.TarInfo("../escape");item.size=1;archive.addfile(item,io.BytesIO(b"x"))
            raw.seek(0)
            with tarfile.open(fileobj=raw) as archive,self.assertRaises(ValueError):
                extract(archive,Path(temporary)/"out")
            self.assertFalse((Path(temporary)/"out").exists())

    def test_three_version_snapshot_is_immutable_and_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);previous=None
            for index in range(4):
                source=f"{index+1:040x}";incoming=root/f"in-{index}";incoming.mkdir();package(incoming,source)
                target=root/f"snapshot-{index}";snapshot(incoming,previous,target,source);previous=target
            versions=json.loads((previous/"latest.json").read_text())["versions"]
            self.assertEqual(len(versions),3)
            self.assertEqual(versions[0]["source_commit"],source)
            with self.assertRaises(ValueError):snapshot(incoming,previous,root/"duplicate",source)


if __name__=="__main__":unittest.main()
