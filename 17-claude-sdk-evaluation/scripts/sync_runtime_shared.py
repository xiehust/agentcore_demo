"""Vendor the shared shopping definitions into runtime_agent/ for deployment.

`agentcore deploy` packages only the entrypoint's own directory (`source_path` in
`.bedrock_agentcore.yaml`), so `src/claude_sdk_evaluation/shopping.py` is not included and
the container cannot import it. This copies it in byte-for-byte.

`tests/test_runtime_agent.py` asserts the copy is identical to the original, so drift
fails the test suite rather than silently deploying a stale prompt — which would make the
recommendation optimize a prompt the reward sessions never ran.

Run this before every `agentcore deploy`. `--check` verifies without writing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "claude_sdk_evaluation" / "shopping.py"
VENDORED = ROOT / "runtime_agent" / "shopping.py"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Exit non-zero if the copy is stale."
    )
    args = parser.parse_args(argv[1:])

    expected = SOURCE.read_bytes()
    current = VENDORED.read_bytes() if VENDORED.exists() else None

    if current == expected:
        print(f"up to date: {VENDORED.relative_to(ROOT)}")
        return 0
    if args.check:
        state = "missing" if current is None else "stale"
        print(f"ERROR: {VENDORED.relative_to(ROOT)} is {state}; run scripts/sync_runtime_shared.py")
        return 1

    VENDORED.parent.mkdir(parents=True, exist_ok=True)
    VENDORED.write_bytes(expected)
    print(f"synced {SOURCE.relative_to(ROOT)} -> {VENDORED.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
