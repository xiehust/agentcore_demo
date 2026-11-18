#!/usr/bin/env python3
"""Extract only bounded, regular benchmark evidence into a new directory."""
import argparse
from pathlib import Path
import tarfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("archive", type=Path)
parser.add_argument("destination", type=Path)
args = parser.parse_args()
if args.destination.exists():
    raise ValueError("refusing to overwrite evidence")
with tarfile.open(args.archive, "r:gz") as archive:
    members = archive.getmembers()
    if len(members) > 1000 or sum(member.size for member in members) > 20 * 1024 * 1024:
        raise ValueError("evidence exceeds extraction limits")
    root = args.destination.resolve()
    for member in members:
        path = (root / member.name).resolve()
        if path != root and root not in path.parents:
            raise ValueError("unsafe archive path")
        if not member.isfile() and not member.isdir():
            raise ValueError("evidence may not contain links or device nodes")
    args.destination.mkdir(parents=True, mode=0o700)
    archive.extractall(args.destination, members=members)
print(f"Extracted {len(members)} entries to {args.destination}")
