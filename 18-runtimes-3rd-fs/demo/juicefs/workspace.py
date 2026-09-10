"""Real Django workspaces: local clone/unzip, then one-object-per-file persistence."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from s5_transfer import RUN_ID, WORKERS, SCHEMA, VERSION, TransferError

REPOSITORY = "https://github.com/django/django.git"
REVISION = "75c4403f07b8ad25893f7832dbe8fc6814b53b2d"
TAG = "5.2.6"
WORKSPACES = ("git-clone", "unzip")
MAX_ENTRIES = 15000
MAX_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 128 * 1024 * 1024

def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative(name: str) -> str:
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or "\\" in name or ":" in name or "\x00" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or len(path.parts) > 40 or len(name.encode()) > 1024):
        raise ValueError("unsafe relative path")
    return name


def validate_manifest(entries: list[dict]) -> None:
    if not entries or len(entries) > MAX_ENTRIES:
        raise ValueError("manifest entry limit")
    paths = {}
    total = 0
    for entry in entries:
        name = safe_relative(entry["path"])
        if name in paths or entry["kind"] not in {"file", "directory", "symlink"}:
            raise ValueError("duplicate path or unsupported file type")
        if entry["mode"] not in {0o644, 0o755}:
            raise ValueError("unsupported permissions")
        if entry["kind"] == "file":
            if not 0 <= entry["size"] <= MAX_FILE_BYTES:
                raise ValueError("file size limit")
            total += entry["size"]
        paths[name] = entry
    if total > MAX_BYTES:
        raise ValueError("workspace byte limit")
    for name, entry in paths.items():
        for parent in PurePosixPath(name).parents:
            if str(parent) != "." and (str(parent) not in paths or paths[str(parent)]["kind"] != "directory"):
                raise ValueError("missing directory or symlink parent")
        if entry["kind"] == "symlink":
            target = entry["target"]
            if not target or target.startswith("/") or "\\" in target or ":" in target:
                raise ValueError("unsafe symlink")
            parts = list(PurePosixPath(name).parent.parts)
            for part in target.split("/"):
                if part == "..":
                    if not parts:
                        raise ValueError("symlink escapes workspace")
                    parts.pop()
                elif part not in {"", "."}:
                    parts.append(part)
            resolved = "/".join(parts)
            if resolved not in paths or paths[resolved]["kind"] != "file":
                raise ValueError("symlink target must be a regular file in this workspace")


def scan_tree(root: Path, *, normalize_modes: bool = False) -> list[dict]:
    entries = []
    for directory, names, files in os.walk(root, followlinks=False):
        for name in sorted(names + files):
            path = Path(directory) / name
            metadata = path.lstat()
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(metadata.st_mode):
                entry = {"path": relative, "kind": "symlink", "target": os.readlink(path), "mode": 0o755}
            elif stat.S_ISDIR(metadata.st_mode):
                if normalize_modes:
                    path.chmod(0o755)
                entry = {"path": relative, "kind": "directory", "mode": stat.S_IMODE(path.lstat().st_mode)}
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_size > MAX_FILE_BYTES:
                    raise ValueError("oversized file")
                if normalize_modes:
                    path.chmod(0o755 if metadata.st_mode & 0o111 else 0o644)
                entry = {"path": relative, "kind": "file", "size": metadata.st_size,
                         "mode": stat.S_IMODE(path.lstat().st_mode), "sha256": file_hash(path)}
            else:
                raise ValueError("unsupported filesystem entry")
            entries.append(entry)
            if len(entries) > MAX_ENTRIES:
                raise ValueError("too many entries")
    entries.sort(key=lambda entry: entry["path"])
    validate_manifest(entries)
    return entries


def manifest_id(entries: list[dict]) -> str:
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def tree_stats(entries: list[dict]) -> dict:
    files = [entry for entry in entries if entry["kind"] == "file"]
    return {"files": len(files), "directories": sum(e["kind"] == "directory" for e in entries),
            "symlinks": sum(e["kind"] == "symlink" for e in entries),
            "total_bytes": sum(e["size"] for e in files),
            "small_files_le_16k": sum(e["size"] <= 16384 for e in files),
            "git_files": sum(e["path"].startswith(".git/") for e in files),
            "manifest_sha256": manifest_id(entries)}


def validate_zip(archive: Path) -> None:
    entries = []
    with zipfile.ZipFile(archive) as source:
        for item in source.infolist():
            mode = item.external_attr >> 16
            name = item.filename.rstrip("/") if item.is_dir() else item.filename
            safe_relative(name)
            if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK}:
                raise ValueError("unsupported archive file type")
            kind = "directory" if item.is_dir() else "symlink" if stat.S_ISLNK(mode) else "file"
            entry = {"path": name, "kind": kind, "mode": 0o755 if kind != "file" or mode & 0o111 else 0o644}
            if kind == "file":
                entry["size"] = item.file_size
            elif kind == "symlink":
                if item.file_size > 1024:
                    raise ValueError("symlink too long")
                entry["target"] = source.read(item).decode("utf-8")
            entries.append(entry)
    validate_manifest(entries)


def git_command(*args, cwd=None) -> subprocess.CompletedProcess:
    # Do not inherit user Git config, credential helpers, filters or hook templates.
    env = {"PATH": os.environ["PATH"], "HOME": "/nonexistent", "LANG": "C.UTF-8",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file:https"}
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                           "-c", "credential.helper=", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, check=True, timeout=180)


def prepare_workspace(kind: str, fixtures: Path, parent: Path) -> dict:
    if kind not in WORKSPACES:
        raise ValueError("unknown workspace")
    info = json.loads((fixtures / "fixture.json").read_text())
    if info["revision"] != REVISION:
        raise ValueError("unexpected fixture revision")
    parent.mkdir(parents=True, exist_ok=True)
    holder = Path(tempfile.mkdtemp(prefix=kind + "-", dir=parent))
    root = holder / "tree"
    if kind == "git-clone":
        revision = git_command("--git-dir=" + str(fixtures / "source.git"), "rev-parse", "HEAD").stdout.strip()
        if revision != REVISION:
            raise ValueError("source repository revision mismatch")
        started = time.perf_counter()
        git_command("clone", "--no-hardlinks", "--template=", str(fixtures / "source.git"), str(root))
        local_seconds = time.perf_counter() - started
        if git_command("rev-parse", "HEAD", cwd=root).stdout.strip() != REVISION:
            raise ValueError("clone revision mismatch")
    else:
        archive = fixtures / "source.zip"
        if file_hash(archive) != info["zip_sha256"]:
            raise ValueError("fixture zip checksum mismatch")
        validate_zip(archive)  # Trusted fixed archive, validate all paths before external unzip.
        started = time.perf_counter()
        subprocess.run(["unzip", "-q", str(archive), "-d", str(root)], check=True,
                       capture_output=True, timeout=180, env={"PATH": os.environ["PATH"], "LANG": "C.UTF-8"})
        local_seconds = time.perf_counter() - started
    started = time.perf_counter()
    # Normalize only the newly created source snapshot, never a restored tree during validation.
    entries = scan_tree(root, normalize_modes=True)
    return {"root": root, "entries": entries, "kind": kind, "fixture": info,
            "local_prepare_seconds": local_seconds, "scan_hash_seconds": time.perf_counter() - started,
            **tree_stats(entries)}

def transfer_workspace(client, bucket: str, base_prefix: str, run_id: str,
                       prepared: dict, phase: str, workers: int, parent: Path,
                       deadline_seconds: int = 720) -> dict:
    if not RUN_ID.fullmatch(run_id) or phase not in {"write", "restore"}:
        raise ValueError("invalid workspace request")
    if type(workers) is not int or workers not in WORKERS:
        raise ValueError("invalid workers")
    entries = prepared["entries"]
    validate_manifest(entries)
    expected_id = manifest_id(entries)
    files = [entry for entry in entries if entry["kind"] == "file"]
    remote = f"s3://{bucket}/{base_prefix.rstrip('/')}/{run_id}/s5cmd/{prepared['kind']}/"
    parent.mkdir(parents=True, exist_ok=True)
    private = Path(tempfile.mkdtemp(prefix="s5-phase-", dir=parent))
    target = private / "tree" if phase == "restore" else None
    manifest_file = private / "manifest.json"
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    if phase == "write":
        manifest_file.write_bytes(encoded)
    pairs = [(prepared["root"] / e["path"], remote + "files/" + e["path"], e["size"])
             if phase == "write" else (remote + "files/" + e["path"], target / e["path"], e["size"])
             for e in files]
    started = time.perf_counter()
    deadline = started + deadline_seconds
    batch_seconds = manifest_seconds = validation_seconds = 0.0
    completed = 0
    published = False
    status = {"success": False, "error_records": 0, "timed_out": False}
    failure = None
    try:
        if phase == "restore":
            result = client.copy(remote + "manifest.json", manifest_file, workers=workers,
                                 timeout=deadline - time.perf_counter(), expected_size=len(encoded))
            manifest_seconds += result["seconds"]
            if file_hash(manifest_file) != expected_id:
                raise ValueError("remote_manifest_mismatch")
            target.mkdir(mode=0o700)
            for entry in entries:
                if entry["kind"] == "directory":
                    directory = target / entry["path"]
                    directory.mkdir(parents=True, exist_ok=True)
                    directory.chmod(entry["mode"])
        status = client.batch(pairs, private / "commands.txt", workers=workers,
                              timeout=deadline - time.perf_counter())
        batch_seconds = status["seconds"]
        completed = status["completed_copies"]
        checked = time.perf_counter()
        if phase == "write":
            if manifest_id(scan_tree(prepared["root"])) != expected_id:
                raise ValueError("source_snapshot_changed")
        else:
            for entry in entries:
                if entry["kind"] == "file":
                    (target / entry["path"]).chmod(entry["mode"])
                elif entry["kind"] == "symlink":
                    os.symlink(entry["target"], target / entry["path"])
            if manifest_id(scan_tree(target)) != expected_id:
                raise ValueError("restored_tree_mismatch")
            if prepared["kind"] == "git-clone" and git_command("rev-parse", "HEAD", cwd=target).stdout.strip() != REVISION:
                raise ValueError("restored_git_revision_mismatch")
        validation_seconds = time.perf_counter() - checked
        if phase == "write":
            result = client.copy(manifest_file, remote + "manifest.json", workers=workers,
                                 timeout=deadline - time.perf_counter())
            manifest_seconds += result["seconds"]
            published = True
    except TransferError as exc:
        status = exc.result
        failure = "s5cmd_failure"
    except (ValueError, TimeoutError, OSError) as exc:
        failure = type(exc).__name__
    elapsed = time.perf_counter() - started
    exceeded = elapsed > deadline_seconds or status.get("timed_out", False)
    success = failure is None and not exceeded and completed == len(files)
    return {"schema": SCHEMA, "engine": "s5cmd", "s5cmd_version": VERSION,
            "success": success, "error": failure, "deadline_exceeded": exceeded,
            "workload": prepared["kind"], "phase": phase, "workers": workers,
            "part_concurrency": 1, "retry_count": 2, "completed_files": completed,
            "file_batch_seconds": batch_seconds, "manifest_seconds": manifest_seconds,
            "validation_seconds": validation_seconds, "wall_seconds": elapsed,
            "file_batch_mib_per_second": sum(e["size"] for e in files) / 1024**2 / batch_seconds if success and batch_seconds else None,
            "manifest_published": published, "transfer_status": status,
            "restored_to": str(target) if target else None,
            "timing": "fresh s5cmd process per batch; subprocess startup/TLS included; wall includes validation; no fsync",
            **tree_stats(entries)}
