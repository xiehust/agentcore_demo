"""Trusted workspace manifests and s5cmd batch orchestration; no cloud calls."""
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo/juicefs"))
import workspace as ws
from s5_transfer import TransferError

class FakeS5:
    def __init__(self):
        self.objects = {}
        self.copies = []
        self.fail = False

    def copy(self, source, destination, **kwargs):
        if kwargs.get("timeout", 1) <= 0:
            raise TimeoutError("expired")
        source, destination = str(source), str(destination)
        self.copies.append((source, destination))
        if source.startswith("s3://"):
            Path(destination).write_bytes(self.objects[source])
        else:
            self.objects[destination] = Path(source).read_bytes()
        return {"success": True, "seconds": 0.001, "completed_copies": 1}

    def batch(self, pairs, command_file, **kwargs):
        if self.fail:
            raise TransferError({"success":False,"timed_out":False,"error_records":1,"exit_code":1})
        for source, destination, size in pairs:
            self.copy(source, destination, **kwargs)
        return {"success":True,"seconds":0.001,"completed_copies":len(pairs),"expected_copies_matched":True,
                "timed_out":False,"error_records":0,"exit_code":0}


@pytest.fixture
def prepared(tmp_path):
    root = tmp_path / "source"
    (root / "sub").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "sub/script.sh").write_bytes(b"echo not executed\n")
    (root / "sub/script.sh").chmod(0o755)
    (root / "zero").write_bytes(b"")
    os.symlink("sub/script.sh", root / "link")
    return {"kind":"unzip","root":root,"entries":ws.scan_tree(root,normalize_modes=True)}


@pytest.mark.parametrize("workers", [8,32,64,128,256])
def test_roundtrip_every_file_and_new_destination(prepared,tmp_path,workers):
    c = FakeS5()
    assert ws.transfer_workspace(c,"a","bench","test-run-001",prepared,"write",workers,tmp_path)["success"]
    assert len(c.objects)==3  # two files + manifest; symlink stored in manifest.
    roots=[]
    for _ in range(2):
        n=len(c.copies)
        previous=os.umask(0o077)
        try: r=ws.transfer_workspace(c,"a","bench","test-run-001",prepared,"restore",workers,tmp_path)
        finally: os.umask(previous)
        assert r["success"] and r["engine"]=="s5cmd" and len(c.copies)-n==3
        root=Path(r["restored_to"])
        assert ws.scan_tree(root)==prepared["entries"]
        assert (root/"sub").stat().st_mode & 0o777 == 0o755
        roots.append(root)
    assert roots[0]!=roots[1]


def test_manifest_corruption_stops_before_files(prepared,tmp_path):
    c=FakeS5()
    ws.transfer_workspace(c,"a","bench","test-run-001",prepared,"write",32,tmp_path)
    key=next(k for k in c.objects if k.endswith("manifest.json"))
    c.objects[key]=b'[{"path":"../escape"}]'
    n=len(c.copies)
    r=ws.transfer_workspace(c,"a","bench","test-run-001",prepared,"restore",32,tmp_path)
    assert not r["success"] and len(c.copies)-n==1


def test_corrupt_file_rejected(prepared,tmp_path):
    c=FakeS5()
    ws.transfer_workspace(c,"a","bench","test-run-001",prepared,"write",32,tmp_path)
    key=next(k for k in c.objects if k.endswith("script.sh")); c.objects[key]=b"bad"
    assert not ws.transfer_workspace(c,"a","bench","test-run-001",prepared,"restore",32,tmp_path)["success"]


def test_partial_batch_does_not_publish(prepared,tmp_path):
    c=FakeS5(); c.fail=True
    r=ws.transfer_workspace(c,"a","bench","test-run-001",prepared,"write",32,tmp_path)
    assert not r["success"] and not r["manifest_published"] and not c.objects


def test_expired_transfer_does_not_publish(prepared,tmp_path):
    c=FakeS5()
    r=ws.transfer_workspace(c,"a","bench","test-run-001",prepared,"write",32,tmp_path,deadline_seconds=-1)
    assert not r["success"] and r["deadline_exceeded"] and not c.objects


@pytest.mark.parametrize("path",["../x","/x","a/../x","a//x","a/./x","C:/x","a\\b"])
def test_unsafe_path(path):
    with pytest.raises(ValueError): ws.safe_relative(path)


def test_unsafe_zip_and_symlink(tmp_path):
    archive=tmp_path/"bad.zip"
    with zipfile.ZipFile(archive,"w") as z: z.writestr("../escape","bad")
    with pytest.raises(ValueError): ws.validate_zip(archive)
    with pytest.raises(ValueError): ws.validate_manifest([{"path":"link","kind":"symlink","mode":0o755,"target":"../outside"}])


def test_directory_validation_does_not_hide_mode_error(prepared):
    (prepared["root"]/"sub").chmod(0o700)
    with pytest.raises(ValueError): ws.scan_tree(prepared["root"])


def test_original_snapshot_required(monkeypatch):
    import importlib.util
    from test_juicefs_sessions import demo_bootstrap
    path=Path(__file__).resolve().parents[1]/"demo/juicefs/agent.py"
    spec=importlib.util.spec_from_file_location("s5_guard_agent",path); agent=importlib.util.module_from_spec(spec); spec.loader.exec_module(agent)
    boot,ctx=demo_bootstrap(); assert agent.invoke(boot,ctx)["success"]
    request={"action":"workspace","phase":"restore","workload":"git-clone","backend":"s3","run_id":"test-run-001","session_token":boot["session_token"]}
    assert agent.invoke(request,ctx)["error"]=="original_session_snapshot_required"
