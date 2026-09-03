#!/usr/bin/env python3
"""Userspace workspace sync — the FUSE-free way to give an agent a remote file tree.

The AgentCore microVM kernel has no FUSE driver (see results/fuse_probe.json), so
instead of *mounting* a third-party store we *synchronise* it around the agent's
lifecycle:

    session start  -> pull(prefix -> /mnt/workspace/<project>)
    after each turn / checkpoint -> push(changed files only)

Only boto3 is needed (any S3-compatible endpoint works via ``endpoint_url``).
Change tracking uses a content-hash manifest so pushes upload deltas only.
The ``SyncAdapter`` protocol keeps the algorithm storage-agnostic: implement it
for SFTP / WebDAV / SMB with the same ``pull``/``push`` semantics.

Combine with ``sessionStorage`` (docs/01) so the local copy survives stop/resume
and the pull on resume is a cheap no-op for unchanged files.

CLI (for demos):
    python3 s3_workspace_sync.py pull s3://bucket/prefix /mnt/workspace/proj
    python3 s3_workspace_sync.py push /mnt/workspace/proj s3://bucket/prefix [--conflict remote_wins]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol

MANIFEST_NAME = ".sync-manifest.json"
DEFAULT_EXCLUDES = (".git/", "node_modules/", "__pycache__/", ".venv/", MANIFEST_NAME)


class SyncAdapter(Protocol):
    """Minimal remote object store interface (implement for S3 / SFTP / WebDAV ...)."""

    def list_remote(self) -> dict[str, str]:
        """Return {relative_path: remote_etag_or_hash}."""

    def download(self, rel_path: str, destination: Path) -> None: ...

    def upload(self, source: Path, rel_path: str) -> None: ...

    def delete(self, rel_path: str) -> None: ...


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_excluded(rel_path: str, excludes: Iterable[str]) -> bool:
    for pattern in excludes:
        if pattern.endswith("/"):
            if rel_path.startswith(pattern) or f"/{pattern}" in f"/{rel_path}":
                return True
        elif rel_path == pattern or rel_path.endswith("/" + pattern):
            return True
    return False


def scan_local(root: Path, excludes: Iterable[str] = DEFAULT_EXCLUDES) -> dict[str, str]:
    """Return {relative_posix_path: sha256} for every regular file under root."""
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(root).as_posix()
        if is_excluded(rel, excludes):
            continue
        result[rel] = sha256_file(path)
    return result


def load_manifest(root: Path) -> dict[str, str]:
    path = root / MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_manifest(root: Path, manifest: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / (MANIFEST_NAME + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, root / MANIFEST_NAME)


@dataclass
class Plan:
    upload: list[str] = field(default_factory=list)
    download: list[str] = field(default_factory=list)
    delete_remote: list[str] = field(default_factory=list)
    delete_local: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any((self.upload, self.download, self.delete_remote, self.delete_local, self.conflicts))


def plan_push(local: dict[str, str], last_synced: dict[str, str], remote: dict[str, str], *,
              conflict: str = "fail") -> Plan:
    """Decide what to upload/delete.

    A file is a *conflict* when it changed locally AND remotely since the last sync
    (remote hash differs from manifest). ``conflict`` = fail | local_wins | remote_wins.
    """
    if conflict not in {"fail", "local_wins", "remote_wins"}:
        raise ValueError("conflict must be fail|local_wins|remote_wins")
    plan = Plan()
    for rel, digest in local.items():
        synced = last_synced.get(rel)
        remote_digest = remote.get(rel)
        if digest == synced and remote_digest in (synced, digest):
            continue  # unchanged
        remote_changed = remote_digest is not None and remote_digest != synced
        if remote_changed and digest != remote_digest and synced is not None:
            if conflict == "fail":
                plan.conflicts.append(rel)
            elif conflict == "local_wins":
                plan.upload.append(rel)
            else:
                plan.download.append(rel)
            continue
        if digest != remote_digest:
            plan.upload.append(rel)
    for rel, synced in last_synced.items():
        if rel not in local and remote.get(rel) == synced:
            plan.delete_remote.append(rel)  # deleted locally, untouched remotely
    return plan


def plan_pull(local: dict[str, str], last_synced: dict[str, str], remote: dict[str, str]) -> Plan:
    """Decide what to download at session start / resume (remote is the source of truth)."""
    plan = Plan()
    for rel, remote_digest in remote.items():
        if local.get(rel) != remote_digest:
            plan.download.append(rel)
    for rel in last_synced:
        if rel not in remote and rel in local:
            plan.delete_local.append(rel)  # deleted remotely since we last synced
    return plan


def apply_plan(plan: Plan, adapter: SyncAdapter, root: Path) -> dict[str, int]:
    if plan.conflicts:
        raise RuntimeError(f"unresolved conflicts: {plan.conflicts}")
    for rel in plan.download:
        destination = root / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        adapter.download(rel, destination)
    for rel in plan.upload:
        adapter.upload(root / rel, rel)
    for rel in plan.delete_remote:
        adapter.delete(rel)
    for rel in plan.delete_local:
        try:
            (root / rel).unlink()
        except FileNotFoundError:
            pass
    return {"downloaded": len(plan.download), "uploaded": len(plan.upload),
            "deleted_remote": len(plan.delete_remote), "deleted_local": len(plan.delete_local)}


def pull(adapter: SyncAdapter, root: Path) -> dict[str, int]:
    remote = adapter.list_remote()
    plan = plan_pull(scan_local(root), load_manifest(root), remote)
    stats = apply_plan(plan, adapter, root)
    save_manifest(root, remote)
    return stats


def push(adapter: SyncAdapter, root: Path, *, conflict: str = "fail") -> dict[str, int]:
    local = scan_local(root)
    remote = adapter.list_remote()
    plan = plan_push(local, load_manifest(root), remote, conflict=conflict)
    stats = apply_plan(plan, adapter, root)
    # After a push the remote mirrors local for uploaded files; downloaded ones mirror remote.
    manifest = dict(remote)
    for rel in plan.upload:
        manifest[rel] = local[rel]
    for rel in plan.delete_remote:
        manifest.pop(rel, None)
    for rel in plan.download:
        manifest[rel] = remote[rel]
    save_manifest(root, manifest)
    return stats


class S3Adapter:
    """S3 (or S3-compatible) adapter. Stores the sha256 in object metadata so hashes compare directly."""

    HASH_META = "sha256"

    def __init__(self, bucket: str, prefix: str, *, client: Any = None, endpoint_url: str | None = None,
                 region: str | None = None):
        if client is None:
            import boto3

            client = boto3.client("s3", region_name=region, endpoint_url=endpoint_url)
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""

    def _key(self, rel: str) -> str:
        return self.prefix + rel

    def list_remote(self) -> dict[str, str]:
        result: dict[str, str] = {}
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                rel = obj["Key"][len(self.prefix):]
                if not rel or rel.endswith("/"):
                    continue
                head = self.client.head_object(Bucket=self.bucket, Key=obj["Key"])
                digest = (head.get("Metadata") or {}).get(self.HASH_META)
                result[rel] = digest or obj["ETag"].strip('"')
        return result

    def download(self, rel_path: str, destination: Path) -> None:
        self.client.download_file(self.bucket, self._key(rel_path), str(destination))

    def upload(self, source: Path, rel_path: str) -> None:
        self.client.upload_file(str(source), self.bucket, self._key(rel_path),
                                ExtraArgs={"Metadata": {self.HASH_META: sha256_file(source)}})

    def delete(self, rel_path: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(rel_path))


def _parse_s3_url(url: str) -> tuple[str, str]:
    if not url.startswith("s3://"):
        raise argparse.ArgumentTypeError("expected s3://bucket/prefix")
    bucket, _, prefix = url[5:].partition("/")
    return bucket, prefix


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("pull"); a.add_argument("remote"); a.add_argument("local")
    b = sub.add_parser("push"); b.add_argument("local"); b.add_argument("remote")
    b.add_argument("--conflict", default="fail", choices=["fail", "local_wins", "remote_wins"])
    for parser in (a, b):
        parser.add_argument("--endpoint-url", default=os.environ.get("S3_ENDPOINT_URL"))
        parser.add_argument("--region", default=os.environ.get("AWS_REGION"))
    args = p.parse_args()
    bucket, prefix = _parse_s3_url(args.remote)
    adapter = S3Adapter(bucket, prefix, endpoint_url=args.endpoint_url, region=args.region)
    root = Path(args.local)
    stats = pull(adapter, root) if args.cmd == "pull" else push(adapter, root, conflict=args.conflict)
    print(json.dumps({"cmd": args.cmd, **stats}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
