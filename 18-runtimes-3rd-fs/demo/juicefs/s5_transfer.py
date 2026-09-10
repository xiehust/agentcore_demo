"""One pinned s5cmd process per batch, explicit tenant credentials, no SDK transfers."""
from __future__ import annotations

import json
import os
import re
import shlex
from collections import Counter
import subprocess
import tempfile
import time
from pathlib import Path

RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
WORKERS = (8, 32, 64, 128, 256)
VERSION = "v2.3.0"
SCHEMA = "s5cmd-workspace-v1"

class TransferError(RuntimeError):
    def __init__(self, result):
        self.result = result
        super().__init__("s5cmd_transfer_failed")


def command_line(parts):
    # s5cmd run uses shell-like word parsing, but never invokes a shell.
    # Reject ambiguity rather than support arbitrary command-file syntax.
    for part in parts:
        if not isinstance(part, str) or any(c in part for c in "\n\r\x00"):
            raise ValueError("unsupported command-file argument")
    return " ".join(shlex.quote(part) for part in parts)


class S5Client:
    def __init__(self, endpoint, region, credentials, ca_bundle=None, *, binary="s5cmd"):
        if endpoint and not endpoint.startswith("https://"):
            raise ValueError("HTTPS required")
        if not all(isinstance(credentials.get(k), str) and credentials[k] for k in ("AccessKeyId", "SecretAccessKey")):
            raise ValueError("explicit credentials required")
        if endpoint is None and not credentials.get("SessionToken"):
            raise ValueError("native S3 requires temporary tenant credentials")
        self.binary = binary
        self.endpoint = endpoint
        # Deliberately do not inherit profiles, endpoint overrides or execution-role variables.
        self.env = {"PATH": os.environ["PATH"], "HOME": "/nonexistent", "LANG": "C.UTF-8",
            "AWS_REGION": region, "AWS_DEFAULT_REGION": region, "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_CONFIG_FILE": "/dev/null", "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "AWS_ACCESS_KEY_ID": credentials["AccessKeyId"], "AWS_SECRET_ACCESS_KEY": credentials["SecretAccessKey"]}
        if credentials.get("SessionToken"):
            self.env["AWS_SESSION_TOKEN"] = credentials["SessionToken"]
        if ca_bundle:
            self.env["AWS_CA_BUNDLE"] = str(ca_bundle)

    def version(self):
        result = subprocess.run([self.binary, "version"], env=self.env, capture_output=True, text=True, timeout=10)
        if result.returncode or not re.search(r"\bv?2\.3\.0\b", result.stdout):
            raise ValueError("expected s5cmd 2.3.0")
        return VERSION

    def execute(self, args, *, workers=8, timeout=720, expected=None):
        if workers not in WORKERS or type(workers) is not int:
            raise ValueError("invalid worker count")
        if timeout <= 0:
            raise TimeoutError("s5cmd deadline exhausted")
        argv = [self.binary, "--json", "--retry-count", "2", "--numworkers", str(workers)]
        if self.endpoint:
            argv += ["--endpoint-url", self.endpoint]
        started = time.perf_counter()
        # Spool output to files: bounded memory even for thousands of per-file JSON records.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            timed_out = False
            try:
                process = subprocess.run(argv + list(args), env=self.env, stdout=out, stderr=err,
                                         timeout=timeout, check=False)
                code = process.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                code = -1  # subprocess.run kills and waits for the child.
            errors, completed, access_denied = 0, 0, False
            observed = Counter()
            for stream in (out, err):
                stream.seek(0)
                for raw in stream:
                    try:
                        record = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        if raw.strip():
                            errors += 1  # Never silently ignore unstructured errors.
                        continue
                    if not isinstance(record, dict):
                        errors += 1
                        continue
                    if record.get("error"):
                        errors += 1
                        text = str(record["error"])
                        access_denied |= bool(re.search(r"\bAccessDenied\b", text)) or (
                            record.get("operation") == "rm" and (
                                text.strip() in {"Access Denied", "Access Denied."}
                                or (text.startswith("User: arn:aws:")
                                    and " is not authorized to perform: s3:DeleteObject on resource: " in text
                                    and "because no identity-based policy allows the s3:DeleteObject action" in text)))
                    elif record.get("success") is True and record.get("operation") == "cp":
                        completed += 1
                        observed[(record.get("source"), record.get("destination"), record.get("object", {}).get("size", 0))] += 1
            matched = expected is None or observed == Counter((str(a), str(b), size) for a, b, size in expected)
            result = {"success": code == 0 and not timed_out and errors == 0 and matched,
                      "expected_copies_matched": matched,
                      "exit_code": code, "timed_out": timed_out, "error_records": errors,
                      "completed_copies": completed, "access_denied": access_denied,
                      "seconds": time.perf_counter() - started}
        return result

    def copy(self, source, destination, *, workers=8, timeout=720, expected_size=None):
        if expected_size is None:
            expected_size = Path(source).stat().st_size
        result = self.execute(["cp", "--raw", "--concurrency", "1", "--no-follow-symlinks", str(source), str(destination)],
                              workers=workers, timeout=timeout, expected=[(source, destination, expected_size)])
        if not result["success"] or result["completed_copies"] != 1:
            raise TransferError(result)
        return result

    def batch(self, pairs, command_file, *, workers, timeout):
        if not pairs:
            raise ValueError("empty batch")
        lines = [command_line(["cp", "--raw", "--concurrency", "1", "--no-follow-symlinks", str(src), str(dst)])
                 for src, dst, size in pairs]
        command_file.write_text("\n".join(lines) + "\n")
        result = self.execute(["run", str(command_file)], workers=workers, timeout=timeout, expected=pairs)
        if not result["success"] or result["completed_copies"] != len(pairs):
            raise TransferError(result)
        return result


def isolation_checks(client, own_bucket, foreign_bucket, own_prefix, foreign_prefix, run_id):
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("invalid run id")
    own = f"s3://{own_bucket}/{own_prefix}/{run_id}/isolation/marker.txt"
    foreign = f"s3://{foreign_bucket}/{foreign_prefix}/{run_id}/isolation/marker.txt"
    checks = {}
    with tempfile.TemporaryDirectory(prefix="s5-isolation-") as root:
        local = Path(root) / "marker.txt"
        local.write_bytes(b"unauthorized-test")
        commands = {
            "foreign_get": ["cp", foreign, str(Path(root) / "download")],
            "foreign_list": ["ls", foreign + "*"],
            "foreign_put": ["cp", str(local), foreign],
            "foreign_delete": ["rm", foreign],
            "copy_foreign_source": ["cp", foreign, own + ".copy"],
            "copy_foreign_destination": ["cp", own, foreign + ".copy"],
        }
        for name, args in commands.items():
            result = client.execute(args, timeout=60)
            checks[name] = {"passed": result["exit_code"] != 0 and not result["timed_out"] and result["access_denied"],
                            "exit_code": result["exit_code"], "access_denied": result["access_denied"]}
    return {"success": all(item["passed"] for item in checks.values()), "checks": checks, "engine": "s5cmd"}
