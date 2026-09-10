#!/usr/bin/env python3
"""Install verified s5cmd ARM64 binary into build/ only; no system package changes."""
import hashlib
import platform
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = "https://github.com/peak/s5cmd/releases/download/v2.3.0/s5cmd_2.3.0_Linux-arm64.tar.gz"
SHA256 = "1439f0d00ecedcd2a2f1f2c6749bbb0152b2257bf5086f29646ec8ae38798e24"


def main():
    if platform.machine() not in {"aarch64", "arm64"}:
        raise ValueError("this demo pins Linux ARM64")
    directory = ROOT / "build/s5cmd"
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / "s5cmd.tar.gz"
    with urllib.request.urlopen(URL, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError("s5cmd archive checksum mismatch")
    archive.write_bytes(data)
    with tarfile.open(archive) as source:
        member = source.getmember("s5cmd")
        if not member.isfile():
            raise ValueError("unexpected archive member")
        (directory / "s5cmd").write_bytes(source.extractfile(member).read())
    (directory / "s5cmd").chmod(0o755)
    print(directory / "s5cmd")


if __name__ == "__main__":
    main()
