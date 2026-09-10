"""Per-microVM session bootstrap from a trusted SigV4 benchmark controller.

This is not a public credential broker. Only the trusted controller may invoke
this demo. Its user/session map must be enforced before calling AgentCore.
"""
from __future__ import annotations

import copy
import hmac
import re
import time
import uuid
from urllib.parse import urlsplit


class SessionBindingError(ValueError):
    """Safe error code only: never include bootstrap credentials."""


class SessionBinding:
    def __init__(self):
        self._state = None
        self.process_id = uuid.uuid4().hex

    @staticmethod
    def _session_id(context):
        session_id = getattr(context, "session_id", None)
        if not isinstance(session_id, str) or not 33 <= len(session_id) <= 256:
            raise SessionBindingError("runtime_session_context_required")
        return session_id

    @staticmethod
    def _validate_storage(value):
        if not isinstance(value, dict):
            raise SessionBindingError("invalid_storage_bootstrap")
        for backend in ("s3", "juicefs"):
            record = value.get(backend, {})
            credentials = record.get("credentials", {})
            for key in ("AccessKeyId", "SecretAccessKey"):
                if not isinstance(credentials.get(key), str) or not credentials[key]:
                    raise SessionBindingError("explicit_storage_credentials_required")
        s3 = value["s3"]
        if not s3["credentials"].get("SessionToken"):
            raise SessionBindingError("temporary_s3_credentials_required")
        if not isinstance(s3.get("expires_at"), (int, float)) or s3["expires_at"] <= time.time() + 60:
            raise SessionBindingError("s3_credentials_expired")
        gateway = value["juicefs"]
        endpoint = urlsplit(gateway.get("endpoint", ""))
        if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password:
            raise SessionBindingError("gateway_https_required")
        if not isinstance(gateway.get("ca_pem"), str) or "BEGIN CERTIFICATE" not in gateway["ca_pem"]:
            raise SessionBindingError("gateway_ca_required")
        bucket = value.get("data_bucket")
        if not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise SessionBindingError("invalid_data_bucket")
        region = value.get("region")
        if not isinstance(region, str) or not re.fullmatch(r"[a-z0-9-]+", region):
            raise SessionBindingError("invalid_region")

    def bind(self, payload, context, *, refresh=False):
        session_id = self._session_id(context)
        if refresh and self._state is None:
            raise SessionBindingError("session_not_initialized")
        if self._state is not None:
            self.authorize(payload, context)
            if payload.get("tenant") != self.tenant:
                raise SessionBindingError("session_tenant_immutable")
        else:
            token = payload.get("session_token")
            if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
                raise SessionBindingError("invalid_session_token")
        if payload.get("session_id") != session_id:
            raise SessionBindingError("session_id_mismatch")
        if payload.get("tenant") not in {"tenant-a", "tenant-b"}:
            raise SessionBindingError("invalid_demo_tenant")
        self._validate_storage(payload.get("storage"))
        if self._state is not None:
            old, new = self._state["storage"], payload["storage"]
            if any(old[key] != new[key] for key in ("region", "data_bucket")) or any(
                old["juicefs"][key] != new["juicefs"][key] for key in ("endpoint", "ca_pem")
            ):
                raise SessionBindingError("storage_target_immutable")
        if self._state is not None and not refresh:
            return self.describe()  # Idempotent initialize never replaces an existing credential set.
        self._state = copy.deepcopy({"session_id": session_id, "tenant": payload["tenant"],
                                     "session_token": payload["session_token"], "storage": payload["storage"]})
        return self.describe()

    def authorize(self, payload, context):
        if self._state is None:
            raise SessionBindingError("session_not_initialized")
        if self._session_id(context) != self._state["session_id"]:
            raise SessionBindingError("session_id_mismatch")
        token = payload.get("session_token")
        if not isinstance(token, str) or not hmac.compare_digest(token, self._state["session_token"]):
            raise SessionBindingError("session_token_mismatch")
        if "tenant" in payload and payload["tenant"] != self.tenant:
            raise SessionBindingError("session_tenant_immutable")

    @property
    def tenant(self):
        if self._state is None:
            raise SessionBindingError("session_not_initialized")
        return self._state["tenant"]

    def storage_config(self):
        if self._state is None:
            raise SessionBindingError("session_not_initialized")
        if self._state["storage"]["s3"]["expires_at"] <= time.time() + 30:
            raise SessionBindingError("s3_credentials_expired")
        return self._state["storage"]

    def describe(self):
        return {"success": True, "tenant": self.tenant, "session_id": self._state["session_id"],
                "process_id": self.process_id, "mode": "one-runtime-multiple-sessions"}
