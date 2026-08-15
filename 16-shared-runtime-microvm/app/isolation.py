"""Per-user isolation primitives for a shared microVM Runtime session.

These functions have no AgentCore or Claude SDK dependency so their path and
identity contracts can be tested locally. Users inside one Runtime session
still share a process and OS trust boundary; these guards are defense in depth
for a cooperative/weak-threat workload, not tenant-grade sandboxing.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PATH_KEYS = frozenset(
    {"file_path", "path", "notebook_path", "cwd", "directory", "dir"}
)
_PATHLESS_TOOLS = frozenset({"TodoWrite"})


class IsolationError(ValueError):
    """Raised when a user identity is invalid."""


def validate_user_id(user_id: object) -> str:
    """Return a valid canonical user ID or raise ``IsolationError``."""
    if not isinstance(user_id, str) or not USER_ID_RE.fullmatch(user_id):
        raise IsolationError("user_id must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    return user_id


def user_slug(user_id: str) -> str:
    """Return a readable, collision-resistant directory name."""
    canonical = validate_user_id(user_id)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    readable = re.sub(r"[^A-Za-z0-9]+", "-", canonical).strip("-").lower()[:24]
    return f"{readable or 'user'}-{digest}"


def workspace_for(users_root: Path, user_id: str) -> Path:
    """Return the absolute workspace path without creating it."""
    return (users_root / user_slug(user_id)).absolute()


def ensure_workspace(users_root: Path, user_id: str) -> Path:
    """Create and return a private per-user workspace."""
    workspace = workspace_for(users_root, user_id)
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    if workspace.is_symlink() or not workspace.is_dir():
        raise IsolationError("per-user workspace must be a real directory")
    workspace.chmod(0o700)
    return workspace


def _iter_path_candidates(tool_name: str, tool_input: dict) -> list[str]:
    candidates = [
        value
        for key, value in tool_input.items()
        if key in _PATH_KEYS and isinstance(value, str) and value
    ]
    # Glob's pattern is itself a path expression. Grep's pattern is content,
    # so it must not be interpreted as a path.
    if tool_name == "Glob":
        pattern = tool_input.get("pattern")
        if isinstance(pattern, str) and pattern:
            candidates.append(pattern)
    return candidates


def _resolves_inside(candidate: str, workspace: Path) -> bool:
    if candidate == "~" or candidate.startswith("~/"):
        candidate = str(workspace) + candidate[1:]
    elif candidate.startswith("~"):
        return False

    path = Path(candidate)
    if not path.is_absolute():
        path = workspace / path
    resolved = path.resolve()
    root = workspace.resolve()
    return resolved == root or root in resolved.parents


def guard_tool_call(tool_name: str, tool_input: object, workspace: Path) -> str | None:
    """Return a denial reason when a tool path escapes ``workspace``."""
    if tool_name in _PATHLESS_TOOLS:
        return None
    if not isinstance(tool_input, dict):
        return "tool input must be an object"
    for candidate in _iter_path_candidates(tool_name, tool_input):
        if not _resolves_inside(candidate, workspace):
            return (
                f"path {candidate!r} resolves outside the per-user workspace; "
                "cross-user access is forbidden"
            )
    return None


def resolve_user_id(header_user: object, payload_user: object) -> str:
    """Validate transport/payload identities and reject disagreement."""
    if header_user is not None and payload_user is not None:
        header = validate_user_id(header_user)
        payload = validate_user_id(payload_user)
        if header != payload:
            raise IsolationError("runtimeUserId header and payload.user_id must match")
        return header
    return validate_user_id(header_user if header_user is not None else payload_user)
