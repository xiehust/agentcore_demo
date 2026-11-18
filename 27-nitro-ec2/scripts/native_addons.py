#!/usr/bin/env python3
"""List host-architecture glibc ELF addons, excluding bundled foreign variants."""
import argparse
from pathlib import Path
import sys


def compatible(file, arch):
    if any(part == "musl" or part.startswith("musl_") for part in file.parts):
        return False
    with file.open("rb") as stream:
        header = stream.read(20)
    machine = {"aarch64": 183, "x86_64": 62}[arch]
    return (len(header) == 20 and header[:4] == b"\x7fELF" and header[4:6] == b"\x02\x01"
            and int.from_bytes(header[18:20], "little") == machine)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--arch", required=True, choices=("aarch64", "x86_64"))
    args = parser.parse_args()
    for file in sorted(args.root.rglob("*.node")):
        if file.is_file() and compatible(file, args.arch):
            sys.stdout.buffer.write(str(file).encode() + b"\0")


if __name__ == "__main__":
    main()
