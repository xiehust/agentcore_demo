"""Deterministic AgentCore storage tool demo; no LLM or arbitrary shell execution."""
from __future__ import annotations

import threading
from pathlib import Path

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from s5_transfer import RUN_ID, S5Client, SCHEMA, isolation_checks
from workspace import WORKSPACES, prepare_workspace, transfer_workspace
from session_binding import SessionBinding, SessionBindingError

app = BedrockAgentCoreApp()
_lock = threading.Lock()
_binding = SessionBinding()

_clients = {}
_workspaces = {}
_saved_workspaces = set()
_restore_count = 0


def storage(backend: str):
    if backend not in {"s3", "juicefs"}:
        raise ValueError("backend must be s3 or juicefs")
    tenant = _binding.tenant
    config = _binding.storage_config()
    region = config["region"]
    if backend not in _clients:
        record = config[backend]
        if backend == "s3":
            client = S5Client(None, region, record["credentials"])
        else:
            # Public trust anchor only. Both backends use explicitly supplied tenant credentials.
            ca = Path("/tmp/juicefs-gateway-ca.pem")
            ca.write_text(record["ca_pem"])
            client = S5Client(record["endpoint"], region, record["credentials"], str(ca))
        client.version()
        _clients[backend] = client
    bucket = config["data_bucket"] if backend == "s3" else tenant
    prefix = f"direct/{tenant}" if backend == "s3" else "bench"
    return _clients[backend], bucket, prefix


@app.entrypoint
def invoke(payload: dict, context=None) -> dict:
    global _restore_count
    # Same runtime artifact, but each microVM owns an independent binding and workspace.
    if not _lock.acquire(blocking=False):
        return {"success": False, "error": "benchmark_already_running"}
    try:
        action = payload.get("action", "workspace")
        if action in {"initialize", "refresh-credentials"}:
            result = _binding.bind(payload, context, refresh=action == "refresh-credentials")
            _clients.clear()
            return {**result, "schema": SCHEMA, "engine": "s5cmd"}
        _binding.authorize(payload, context)
        if action == "ping":
            return _binding.describe()
        if action in {"prepare-workspace", "workspace"}:
            kind = payload["workload"]
            if kind not in WORKSPACES:
                raise ValueError("unknown workspace")
            if action == "workspace" and payload.get("phase") == "restore":
                snapshot = (payload.get("backend"), payload.get("run_id"), kind)
                if kind not in _workspaces or snapshot not in _saved_workspaces:
                    return {"success": False, "error": "original_session_snapshot_required"}
            if kind not in _workspaces:
                _workspaces[kind] = prepare_workspace(kind, Path("/opt/fixtures"), Path("/tmp/jfs-workspaces"))
            if action == "prepare-workspace":
                return {"success": True, **{key: value for key, value in _workspaces[kind].items()
                                            if key not in {"root", "entries"}}}
        backend = payload["backend"]
        client, bucket, prefix = storage(backend)
        run_id = payload["run_id"]
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError("invalid run_id")
        if action == "workspace":
            if payload["phase"] == "restore":
                import shutil
                if _restore_count >= 32 or shutil.disk_usage("/tmp").free < 600 * 1024 * 1024:
                    raise ValueError("workspace disk budget exhausted; use a fresh session")
                _restore_count += 1
            snapshot = (backend, run_id, payload["workload"])
            if payload["phase"] == "write":
                _saved_workspaces.discard(snapshot)
            result = transfer_workspace(
                client, bucket, prefix, run_id, _workspaces[payload["workload"]],
                payload["phase"], payload.get("workers", 32), Path("/tmp/jfs-restores"))
            if payload["phase"] == "write" and result["success"]:
                _saved_workspaces.add(snapshot)
            return {"backend": backend, **result}
        remote = f"s3://{bucket}/{prefix}/{run_id}/isolation/marker.txt"
        expected = _binding.tenant.encode()
        if action in {"seed", "verify-marker"}:
            import tempfile
            with tempfile.TemporaryDirectory(prefix="s5-marker-") as folder:
                local = Path(folder) / "marker.txt"
                if action == "seed":
                    local.write_bytes(expected)
                    client.copy(local, remote, timeout=60)
                    return {"success": True}
                client.copy(remote, local, timeout=60, expected_size=len(expected))
                return {"success": local.read_bytes() == expected}
        if action == "isolation":
            other = "tenant-b" if _binding.tenant == "tenant-a" else "tenant-a"
            foreign_bucket = _binding.storage_config()["data_bucket"] if backend == "s3" else other
            foreign_prefix = f"direct/{other}" if backend == "s3" else "bench"
            return isolation_checks(client, bucket, foreign_bucket, prefix, foreign_prefix, run_id)
        raise ValueError("unknown action")
    except SessionBindingError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        # Do not echo payload, credentials, SDK messages or request URLs.
        return {"success": False, "error": type(exc).__name__}
    finally:
        _lock.release()

if __name__ == "__main__":
    app.run()
