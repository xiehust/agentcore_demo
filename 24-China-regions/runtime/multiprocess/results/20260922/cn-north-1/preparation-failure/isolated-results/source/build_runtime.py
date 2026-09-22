#!/usr/bin/env python3
"""Build, validate and push the ARM64 test image on the Beijing EC2."""
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import traceback
import urllib.request

import boto3

ROOT = Path(__file__).resolve().parent


def main():
    cfg = json.loads((ROOT / "build_config.json").read_text())
    assert cfg["region"] in ("cn-north-1", "cn-northwest-1")
    s3 = boto3.client("s3", region_name=cfg["region"])
    ecr = boto3.client("ecr", region_name=cfg["region"])
    status = {"phase": "starting", "at": datetime.now(timezone.utc).isoformat()}

    def publish(phase):
        status.update(phase=phase, at=datetime.now(timezone.utc).isoformat())
        s3.put_object(Bucket=cfg["bucket"], Key="results/build_status.json",
                      Body=json.dumps(status, default=str).encode())
        print(json.dumps(status, default=str), flush=True)

    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        token = opener.open(urllib.request.Request("http://169.254.169.254/latest/api/token",
            method="PUT", headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"}), timeout=3).read().decode()
        identity = json.loads(opener.open(urllib.request.Request(
            "http://169.254.169.254/latest/dynamic/instance-identity/document",
            headers={"X-aws-ec2-metadata-token": token}), timeout=3).read())
        assert identity["region"] == cfg["region"] and identity["instanceId"] == cfg["instance_id"]
        assert identity["architecture"] == "arm64"
        status["instance_identity"] = identity
        tag = hashlib.sha256((ROOT / "app.py").read_bytes() + (ROOT / "Dockerfile").read_bytes()).hexdigest()[:16]
        image = cfg["repository"]["repositoryUri"] + ":" + tag
        build_command = ["docker", "build", "--platform", "linux/arm64", "-t", image]
        build_env = None
        if cfg.get("offline_base"):
            base = cfg["offline_base"]
            archive = ROOT / "offline-base.tar.gz"
            publish("loading_verified_base_on_beijing_ec2")
            s3.download_file(cfg["bucket"], base["s3_key"], str(archive))
            assert hashlib.sha256(archive.read_bytes()).hexdigest() == base["archive_sha256"]
            subprocess.run(["docker", "load", "-i", str(archive)], check=True, timeout=120)
            loaded = json.loads(subprocess.check_output(["docker", "image", "inspect", base["image_id"]]))[0]
            # Docker versions normalize optional inspect fields differently.
            # Preserve content-addressed image identity and runtime-relevant config.
            assert loaded["Id"] == base["image_id"] and loaded["RootFS"] == base["rootfs"]
            for key in ("Env", "Cmd", "Entrypoint", "User", "WorkingDir"):
                assert (loaded["Config"].get(key) or None) == (base["config"].get(key) or None), key
            status["inspect_config_differences"] = {
                key: {"source": base["config"].get(key), "loaded": loaded["Config"].get(key)}
                for key in set(base["config"]) | set(loaded["Config"])
                if base["config"].get(key) != loaded["Config"].get(key)}
            subprocess.run(["docker", "tag", base["image_id"], base["local_tag"]], check=True)
            original = (ROOT / "Dockerfile").read_text().splitlines()
            (ROOT / "Dockerfile.offline").write_text(
                "FROM " + base["local_tag"] + "\n" + "\n".join(original[1:]) + "\n")
            build_command += ["--pull=false", "-f", str(ROOT / "Dockerfile.offline")]
            build_env = {**os.environ, "DOCKER_BUILDKIT": "0"}
            status["offline_base_verified"] = base["image_id"]
        publish("building_on_beijing_ec2")
        subprocess.run(build_command + [str(ROOT)], env=build_env, check=True, timeout=360)
        inspect = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
        assert inspect["Architecture"] == "arm64"
        publish("testing_container_on_beijing_ec2")
        cid = subprocess.check_output(["docker", "run", "--rm", "-d", "-p", "127.0.0.1::8080", image],
                                      text=True).strip()
        replies = []
        try:
            address = subprocess.check_output(["docker", "port", cid, "8080/tcp"], text=True).strip()
            url = "http://" + address
            for _ in range(30):
                try:
                    ping = json.loads(urllib.request.urlopen(url + "/ping", timeout=1).read())
                    break
                except OSError:
                    time.sleep(0.2)
            else:
                raise RuntimeError("Container failed readiness check")
            for i in range(3):
                request = urllib.request.Request(url + "/invocations",
                    data=json.dumps({"nonce": str(i)}).encode(),
                    headers={"Content-Type": "application/json"})
                replies.append(json.loads(urllib.request.urlopen(request, timeout=2).read()))
            assert ping["status"] == "Healthy"
            assert [r["request_index"] for r in replies] == [1, 2, 3]
            assert len({r["instance_id"] for r in replies}) == 1
        finally:
            subprocess.run(["docker", "stop", cid], check=True)
        publish("pushing_from_beijing_ec2")
        auth = ecr.get_authorization_token()["authorizationData"][0]
        user, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
        auth_dir = ROOT / "docker-auth"
        auth_dir.mkdir(mode=0o700, exist_ok=True)
        try:
            subprocess.run(["docker", "--config", str(auth_dir), "login", "--username", user,
                            "--password-stdin", auth["proxyEndpoint"]], input=password, text=True, check=True)
            subprocess.run(["docker", "--config", str(auth_dir), "push", image], check=True, timeout=300)
        finally:
            subprocess.run(["docker", "--config", str(auth_dir), "logout", auth["proxyEndpoint"]])
            shutil.rmtree(auth_dir)
        description = ecr.describe_images(repositoryName=cfg["repository"]["repositoryName"],
                                          imageIds=[{"imageTag": tag}])["imageDetails"][0]
        result = {"identity": identity, "image": {
            "uri": image, "immutable_uri": cfg["repository"]["repositoryUri"] + "@" + description["imageDigest"],
            "ecr": description, "local_size_bytes": inspect["Size"], "architecture": "arm64"},
            "container_checks": replies, "app_sha256": hashlib.sha256((ROOT / "app.py").read_bytes()).hexdigest(),
            "rootfs": inspect["RootFS"], "config": inspect["Config"],
            "offline_base": cfg.get("offline_base")}
        s3.put_object(Bucket=cfg["bucket"], Key="results/build_result.json",
                      Body=json.dumps(result, default=str).encode())
        publish("completed")
    except Exception as exc:
        status["error"] = str(exc)
        status["traceback"] = traceback.format_exc()
        publish("failed")
        raise
    finally:
        if (ROOT / "build.log").exists():
            s3.upload_file(str(ROOT / "build.log"), cfg["bucket"], "results/build.log")


if __name__ == "__main__":
    main()
