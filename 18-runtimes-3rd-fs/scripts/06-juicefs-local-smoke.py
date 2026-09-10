#!/usr/bin/env python3
"""Real local JuiceFS TLS/IAM smoke test; file backend, NOT an AWS performance test."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo/juicefs"))
from s5_transfer import S5Client, isolation_checks
from workspace import scan_tree, transfer_workspace

def download(url, destination, expected_sha=None):
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read()
    if expected_sha and hashlib.sha256(data).hexdigest() != expected_sha:
        raise ValueError("download checksum mismatch")
    destination.write_bytes(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "results/s5cmd-local-smoke.json")
    parser.add_argument("--workspace", action="store_true", help="also persist/restore both full Django trees")
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError("output already exists")
    if platform.machine() not in {"aarch64", "arm64"}:
        raise ValueError("this pinned smoke test requires Linux ARM64")
    os.umask(0o077)
    (ROOT / "build").mkdir(exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="juicefs-smoke-", dir=ROOT / "build"))
    result = {"engine": "s5cmd", "s5cmd_version": "v2.3.0", "success": False, "scope": "localhost TLS/IAM with file data backend; NOT AWS benchmark", "artifacts": str(home)}
    process = None
    log = (home / "gateway.log").open("w")
    try:
        archive = home / "juicefs.tar.gz"
        download("https://github.com/juicedata/juicefs/releases/download/v1.4.1/juicefs-1.4.1-linux-arm64.tar.gz", archive,
                 "1015ade83a7a93180a29f6c93ee5780a3eda52522331934ef2ef0cc0921995fd")
        with tarfile.open(archive) as tar:
            member = tar.getmember("juicefs")
            if not member.isfile():
                raise ValueError("binary is not a regular file")
            (home / "juicefs").write_bytes(tar.extractfile(member).read())
        download("https://dl.min.io/client/mc/release/linux-arm64/archive/mc.RELEASE.2021-04-22T17-40-00Z", home / "mc",
                 "23c98c585da3b3c6587445ce85e949d2618f80ab187641059007757ce3167df3")
        for binary in ("mc", "juicefs"):
            (home / binary).chmod(0o700)
        env = {"HOME": str(home), "PATH": os.environ["PATH"], "AWS_EC2_METADATA_DISABLED": "true",
               "MINIO_ROOT_USER": "admin-" + secrets.token_hex(8), "MINIO_ROOT_PASSWORD": secrets.token_hex(24)}
        def command(*args):
            completed = subprocess.run(list(args), env=env, stdout=log, stderr=log, timeout=120)
            if completed.returncode:
                raise RuntimeError("local setup command failed; inspect private artifact log")
        meta = "sqlite3://" + str(home / "metadata.db")
        command(str(home / "juicefs"), "format", "--storage", "file", "--bucket", str(home / "objects"), meta, "shared-demo")
        certs = home / ".minio/certs"
        certs.mkdir(parents=True)
        command("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                "-keyout", str(certs / "private.key"), "-out", str(certs / "public.crt"),
                "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        endpoint = f"https://127.0.0.1:{port}"
        process = subprocess.Popen([str(home / "juicefs"), "gateway", "--multi-buckets", "--object-meta", "--keep-etag",
            "--no-banner", "--cache-dir", str(home / "cache"), "--cache-size", "128", "--refresh-iam-interval", "1s",
            meta, f"127.0.0.1:{port}"], env=env, stdout=log, stderr=log)
        import ssl
        context = ssl.create_default_context(cafile=str(certs / "public.crt"))
        for attempt in range(60):
            if process.poll() is not None:
                raise RuntimeError("gateway exited")
            try:
                with urllib.request.urlopen(endpoint + "/minio/health/live", context=context, timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.5)
        else:
            raise TimeoutError("gateway TLS readiness failed")
        ca = home / ".mc/certs/CAs"
        ca.mkdir(parents=True)
        (ca / "demo.crt").write_bytes((certs / "public.crt").read_bytes())
        command(str(home / "mc"), "alias", "set", "local", endpoint, env["MINIO_ROOT_USER"], env["MINIO_ROOT_PASSWORD"])
        clients = {}
        binary = str(ROOT / "build/s5cmd/s5cmd")
        for tenant in ("tenant-a", "tenant-b"):
            creds = {"AccessKeyId": tenant, "SecretAccessKey": secrets.token_hex(24)}
            command(str(home / "mc"), "mb", "local/" + tenant)
            document = {"Version":"2012-10-17","Statement":[
                {"Effect":"Allow","Action":["s3:ListBucket"],"Resource":[f"arn:aws:s3:::{tenant}"],
                 "Condition":{"StringLike":{"s3:prefix":["bench/*"]}}},
                {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject"],"Resource":[f"arn:aws:s3:::{tenant}/bench/*"]}]}
            policy_path = home / (tenant + ".json")
            policy_path.write_text(json.dumps(document))
            command(str(home / "mc"), "admin", "user", "add", "local", creds["AccessKeyId"], creds["SecretAccessKey"])
            command(str(home / "mc"), "admin", "policy", "add", "local", tenant, str(policy_path))
            command(str(home / "mc"), "admin", "policy", "set", "local", tenant, "user=" + tenant)
            clients[tenant] = S5Client(endpoint, "us-west-2", creds, str(certs / "public.crt"), binary=binary)
            clients[tenant].version()
        run_id = "local-smoke-001"
        for tenant, client in clients.items():
            marker = home / (tenant + "-marker")
            marker.write_bytes(tenant.encode())
            client.copy(marker, f"s3://{tenant}/bench/{run_id}/isolation/marker.txt", timeout=60)
        result["isolation"] = {}
        for tenant, client in clients.items():
            other = "tenant-b" if tenant == "tenant-a" else "tenant-a"
            result["isolation"][tenant] = isolation_checks(client, tenant, other, "bench", "bench", run_id)
            assert result["isolation"][tenant]["success"]
        for tenant, client in clients.items():
            downloaded = home / (tenant + "-download")
            client.copy(f"s3://{tenant}/bench/{run_id}/isolation/marker.txt", downloaded, timeout=60, expected_size=len(tenant.encode()))
            assert downloaded.read_bytes() == tenant.encode()
        source = home / "test-tree"
        (source / ".git").mkdir(parents=True)
        for index in range(105):
            (source / f"file {index}.txt").write_bytes(bytes([index]) * 4096)
        (source / ".git/HEAD").write_text("test")
        (source / "glob[1]*.txt").write_bytes(b"literal wildcard")
        os.symlink("file 0.txt", source / "link")
        prepared = {"root": source, "kind": "unzip", "entries": scan_tree(source, normalize_modes=True)}
        result["operations"] = []
        for phase in ("write", "restore", "restore"):
            measurement = transfer_workspace(clients["tenant-a"], "tenant-a", "bench", run_id,
                                             prepared, phase, 32, home / "test-restores")
            result["operations"].append(measurement)
            assert measurement["success"], phase
        if args.workspace:
            from workspace import prepare_workspace
            result["workspaces"] = []
            for kind in ("git-clone", "unzip"):
                prepared = prepare_workspace(kind, ROOT / "build/juicefs-fixtures", home / "workspaces")
                for phase in ("write", "restore"):
                    measurement = transfer_workspace(clients["tenant-a"], "tenant-a", "bench", run_id,
                        prepared, phase, 32, home / "restores")
                    result["workspaces"].append(measurement)
                    if not measurement["success"]:
                        raise RuntimeError("workspace persistence/restore failed")
        result["success"] = True
    except Exception as exc:
        result["error"] = type(exc).__name__
        raise
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log.close()
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n")
        print(f"Local smoke success={result['success']}; result={args.out}; private artifacts={home}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
