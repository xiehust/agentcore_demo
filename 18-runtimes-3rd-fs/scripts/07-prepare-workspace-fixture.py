#!/usr/bin/env python3
"""Fetch a fixed Django snapshot for offline clone/unzip inside AgentCore."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo/juicefs"))
from workspace import REPOSITORY, REVISION, TAG, file_hash, git_command, prepare_workspace, validate_zip

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build/juicefs-fixtures")
    parser.add_argument("--report", type=Path, default=ROOT / "results/juicefs-workspace-fixture.json")
    args = parser.parse_args()
    if args.output.exists() or args.report.exists():
        raise ValueError("fixture/report already exists; preserve it or choose another path")
    output = args.output.resolve()
    output.mkdir(parents=True)
    started = time.perf_counter()
    git_command("clone", "--bare", "--depth", "1", "--single-branch", "--branch", TAG,
                "--template=", REPOSITORY, str(output / "source.git"))
    clone_seconds = time.perf_counter() - started
    actual = git_command("--git-dir=" + str(output / "source.git"), "rev-parse", "HEAD").stdout.strip()
    if actual != REVISION:
        raise ValueError("upstream tag moved: refusing unpinned fixture")
    git_command("--git-dir=" + str(output / "source.git"), "fsck", "--full")
    archive = output / "source.zip"
    git_command("--git-dir=" + str(output / "source.git"), "archive", "--format=zip", "--output=" + str(archive), REVISION)
    validate_zip(archive)
    record = {"repository": REPOSITORY, "tag": TAG, "revision": REVISION,
              "history": "shallow depth=1 bare clone; not full history",
              "zip_sha256": file_hash(archive), "zip_bytes": archive.stat().st_size,
              "source_clone_seconds_including_github": clone_seconds,
              "git_version": git_command("--version").stdout.strip()}
    (output / "fixture.json").write_text(json.dumps(record, indent=2) + "\n")
    measurements = []
    for kind in ("git-clone", "unzip"):
        prepared = prepare_workspace(kind, output, ROOT / "build/juicefs-workspaces")
        measurements.append({key: value for key, value in prepared.items() if key not in {"root", "entries", "fixture"}})
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({"fixture": record, "local_baselines": measurements,
        "scope": "developer host local clone/unzip only; no S3 or AgentCore performance measurement"}, indent=2) + "\n")
    print(json.dumps({"fixture": record, "local_baselines": measurements}, indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main())
