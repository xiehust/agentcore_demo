import hashlib
from pathlib import Path

import pytest

import s3_workspace_sync as sync


class FakeRemote:
    """In-memory SyncAdapter."""

    def __init__(self, files: dict[str, bytes] | None = None):
        self.files = dict(files or {})
        self.ops: list[tuple[str, str]] = []

    def list_remote(self):
        return {k: hashlib.sha256(v).hexdigest() for k, v in self.files.items()}

    def download(self, rel, destination: Path):
        destination.write_bytes(self.files[rel]); self.ops.append(("download", rel))

    def upload(self, source: Path, rel):
        self.files[rel] = source.read_bytes(); self.ops.append(("upload", rel))

    def delete(self, rel):
        self.files.pop(rel, None); self.ops.append(("delete", rel))


def test_pull_then_push_roundtrip(tmp_path):
    remote = FakeRemote({"README.md": b"hello", "src/app.py": b"print(1)"})
    assert sync.pull(remote, tmp_path) == {"downloaded": 2, "uploaded": 0, "deleted_remote": 0, "deleted_local": 0}
    assert (tmp_path / "src/app.py").read_bytes() == b"print(1)"

    (tmp_path / "src/app.py").write_bytes(b"print(2)")
    (tmp_path / "new.txt").write_bytes(b"n")
    (tmp_path / "README.md").unlink()
    (tmp_path / ".git").mkdir(); (tmp_path / ".git/HEAD").write_text("ref")  # excluded
    stats = sync.push(remote, tmp_path)
    assert stats == {"downloaded": 0, "uploaded": 2, "deleted_remote": 1, "deleted_local": 0}
    assert remote.files == {"src/app.py": b"print(2)", "new.txt": b"n"}

    # second push is a no-op
    assert sync.push(remote, tmp_path)["uploaded"] == 0


def test_push_conflict_detection(tmp_path):
    remote = FakeRemote({"a.txt": b"v1"})
    sync.pull(remote, tmp_path)
    (tmp_path / "a.txt").write_bytes(b"local")
    remote.files["a.txt"] = b"remote"  # changed on both sides
    with pytest.raises(RuntimeError, match="conflicts"):
        sync.push(remote, tmp_path)
    sync.push(remote, tmp_path, conflict="remote_wins")
    assert (tmp_path / "a.txt").read_bytes() == b"remote"
    (tmp_path / "a.txt").write_bytes(b"local2"); remote.files["a.txt"] = b"remote2"
    sync.push(remote, tmp_path, conflict="local_wins")
    assert remote.files["a.txt"] == b"local2"


def test_pull_removes_files_deleted_remotely(tmp_path):
    remote = FakeRemote({"a": b"1", "b": b"2"})
    sync.pull(remote, tmp_path)
    del remote.files["b"]
    stats = sync.pull(remote, tmp_path)
    assert stats["deleted_local"] == 1 and not (tmp_path / "b").exists()


def test_excludes():
    assert sync.is_excluded("node_modules/x/y.js", sync.DEFAULT_EXCLUDES)
    assert sync.is_excluded("pkg/__pycache__/m.pyc", sync.DEFAULT_EXCLUDES)
    assert sync.is_excluded(".sync-manifest.json", sync.DEFAULT_EXCLUDES)
    assert not sync.is_excluded("src/main.py", sync.DEFAULT_EXCLUDES)


def test_plan_push_invalid_conflict_mode():
    with pytest.raises(ValueError):
        sync.plan_push({}, {}, {}, conflict="whatever")
