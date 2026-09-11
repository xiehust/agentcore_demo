"""Repeat only the 500mb c200 burst without modifying the original runner."""
import argparse
import contextlib
import hashlib
import os
from pathlib import Path
import sys

import coldstart_v2_c200 as benchmark


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="new output directory")
    args = parser.parse_args()
    os.umask(0o077)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    (out / "raw").mkdir()
    source = Path(__file__).resolve()
    benchmark.original.save(out / "retest.json", {
        "sizes": ["500mb"], "concurrency": 200, "rounds": 1,
        "source": str(source.relative_to(benchmark.HERE.parent)),
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "comparison_run": "results/coldstart_v2_c200_2026-09-11",
        "note": "Fresh client process, one smoke, 180s settle, one burst; no retries or quota changes."})
    previous = benchmark.SIZES
    try:
        benchmark.SIZES = ("500mb",)
        with (out / "run.log").open("w", buffering=1) as stream:
            with contextlib.redirect_stdout(benchmark.original.Tee(sys.stdout, stream)):
                with contextlib.redirect_stderr(benchmark.original.Tee(sys.stderr, stream)):
                    return benchmark.run(out)
    finally:
        benchmark.SIZES = previous


if __name__ == "__main__":
    sys.exit(main())
