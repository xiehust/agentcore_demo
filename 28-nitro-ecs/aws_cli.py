"""Bounded AWS CLI calls; no automatic replay of ambiguous mutations."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(data, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class AWS:
    def __init__(self, region, evidence=None):
        self.region = region
        self.evidence = Path(evidence) if evidence else None
        self.counter = 0

    def call(self, service, operation, request=None, timeout=120):
        command = ["aws", "--region", self.region, "--output", "json", service, operation]
        if request is not None:
            command += ["--cli-input-json", json.dumps(request)]
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout,
            env={**os.environ, "AWS_PAGER": "", "AWS_MAX_ATTEMPTS": "1"})
        if result.returncode:
            raise RuntimeError(f"{service}.{operation}: {result.stderr.strip()}")
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        if self.evidence:
            self.counter += 1
            # Only persist responses, never IAM credential/token request payloads.
            write_json(self.evidence / f"{self.counter:04d}-{service}-{operation}.json", data)
        return data

    def invoke(self, function, payload, output):
        result = subprocess.run(["aws", "--region", self.region, "lambda", "invoke", "--function-name", function,
            "--cli-binary-format", "raw-in-base64-out", "--payload", json.dumps(payload), str(output)],
            capture_output=True, text=True, timeout=200,
            env={**os.environ, "AWS_PAGER": "", "AWS_MAX_ATTEMPTS": "1"})
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
        metadata = json.loads(result.stdout)
        response = json.loads(Path(output).read_text())
        Path(output).chmod(0o600)
        if metadata.get("FunctionError"):
            raise RuntimeError("VPC probe failed: " + response.get("errorMessage", "unknown error"))
        return response
