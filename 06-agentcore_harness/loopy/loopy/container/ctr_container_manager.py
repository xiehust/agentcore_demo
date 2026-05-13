"""CinC container manager — manages a customer sibling container via containerd (ctr)."""

import base64
import logging
import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from typing import Any, Optional

import boto3
from botocore.client import BaseClient

from loopy.abstract import FileIO, LoopyContainerManager
from loopy.api_model.request import Credentials
from loopy.util.constants import CTR_SOCKET, CUSTOMER_CONTAINER_NAME
from botocore.exceptions import ClientError

from loopy.container.stream import stream_subprocess
from loopy.container.types import StreamChunk

logger = logging.getLogger(__name__)


class AccessDeniedException(Exception):
    """Raised when ECR access is denied — maps to 403 via DP error_type handling."""


class CinCFileIO(FileIO):
    def __init__(self, cm: "CtrContainerManager") -> None:
        self._cm = cm

    def _run(self, command: str) -> str:
        result = self._cm.run(command)
        if result["exit_code"] != 0:
            raise RuntimeError(f"Command failed (exit {result['exit_code']}): {result['stderr']}")
        return result["stdout"]

    def read(self, path: str) -> str:
        return self._run(f"cat {path}")

    def write(self, path: str, content: str) -> None:
        escaped = content.replace("'", "'\\''")
        self._run(f"printf '%s' '{escaped}' | cat > {path}")

    def exists(self, path: str) -> bool:
        return self._cm.run(f"test -e {path}")["exit_code"] == 0

    def is_dir(self, path: str) -> bool:
        return self._cm.run(f"test -d {path}")["exit_code"] == 0

    def listdir(self, path: str) -> str:
        return self._run(f"ls {path}")

    def mkdir_parents(self, path: str) -> None:
        self._run(f"mkdir -p $(dirname {path})")


class CtrContainerManager(LoopyContainerManager):
    """CinC — runs commands and file operations in a customer sibling container via containerd (ctr)."""

    def __init__(self, container_uri: str, ecr_client: BaseClient, filesystem_mount_paths: list[str], socket: str = CTR_SOCKET) -> None:
        self._uri = container_uri
        self._ecr = ecr_client
        self._socket = socket
        self._filesystem_mount_paths = filesystem_mount_paths
        self._started = False

    def _ctr(self, *args: str, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(["/usr/local/bin/ctr", "-a", self._socket, *args], **kwargs)

    def _ctr_exec_cmd(self, command: str) -> list[str]:
        exec_id = f"exec-{uuid.uuid4().hex[:12]}"
        return [
            "/usr/local/bin/ctr", "-a", self._socket, "tasks", "exec",
            "--exec-id", exec_id, CUSTOMER_CONTAINER_NAME, "/bin/bash", "-c", command,
        ]

    async def ensure_started(self, credentials: Optional[Credentials] = None) -> None:
        if self._started:
            return
        logger.info("Starting customer container: %s", self._uri)

        self._pull_image()

        # Clean up any stale container from a previous process lifetime (e.g., crash/restart).
        # ctr run is not idempotent — it fails if a container with the same name already exists.
        # Sequence: kill task → delete task → remove container. All ignore errors if nothing exists.
        self._ctr("tasks", "kill", CUSTOMER_CONTAINER_NAME, capture_output=True)
        self._ctr("tasks", "delete", CUSTOMER_CONTAINER_NAME, capture_output=True)
        self._ctr("containers", "rm", CUSTOMER_CONTAINER_NAME, capture_output=True)

        aws_dir = os.path.expanduser("~/.aws")
        run_args = ["run", "-d", "--net-host"]
        if os.path.exists(aws_dir):
            run_args.append(f"--mount=type=bind,src={aws_dir},dst=/root/.aws,options=rbind:ro")
        for mount_path in self._filesystem_mount_paths:
            os.makedirs(mount_path, exist_ok=True)
            run_args.append(f"--mount=type=bind,src={mount_path},dst={mount_path},options=rbind:rw")
        run_args.extend([self._uri, CUSTOMER_CONTAINER_NAME, "/bin/sh", "-c", "sleep infinity"])

        try:
            self._ctr(*run_args, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to start customer container: {e.stderr}") from e
        logger.info("Customer container started: %s", CUSTOMER_CONTAINER_NAME)

        self._started = True

    def _pull_image(self) -> None:
        try:
            token_resp = self._ecr.get_authorization_token()
        except ClientError as e:
            if e.response["Error"]["Code"] == "AccessDeniedException":
                raise AccessDeniedException(f"ECR access denied for {self._uri}: {e}") from e
            raise
        auth_data = token_resp["authorizationData"]
        auth = auth_data if isinstance(auth_data, dict) else auth_data[0]
        token = base64.b64decode(auth["authorizationToken"]).decode("utf-8")
        self._ctr(
            "images", "pull", "--user", token, self._uri,
            check=True, capture_output=True, text=True,
        )
        logger.info("Pulled customer image: %s", self._uri)

    def run(self, command: str, timeout: int = 300) -> dict[str, Any]:
        try:
            result = subprocess.run(self._ctr_exec_cmd(command), capture_output=True, text=True, timeout=timeout)
            return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode}
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": f"Command timed out after {timeout} seconds", "exit_code": -1}

    async def run_async(self, command: str, timeout: int = 300) -> AsyncIterator[StreamChunk]:
        async for chunk in stream_subprocess(self._ctr_exec_cmd(command), timeout):
            yield chunk

    @property
    def is_customer_container(self) -> bool:
        return True

    @property
    def file_io(self) -> FileIO:
        return CinCFileIO(self)

def create_ecr_client(container_uri: str) -> BaseClient:
    """Select private ECR or public ECR client based on the image URI."""
    if container_uri.startswith("public.ecr.aws/"):
        return boto3.client("ecr-public", region_name="us-east-1")
    region = container_uri.split(".dkr.ecr.")[1].split(".")[0]
    return boto3.client("ecr", region_name=region)
