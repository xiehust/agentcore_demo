"""Per-user isolation primitives for the shared-runtime-session demo.

Pure functions only (no server / SDK imports) so they can be unit-tested
without the Claude Agent SDK or AgentCore installed.

Security model
--------------
Many users share ONE AgentCore runtime session (= one container process).
Isolation is enforced at the application layer:

1. ``validate_user_id``  — strict allowlist regex, rejects anything unusual.
2. ``workspace_for``     — per-user directory whose name embeds a SHA-256
   prefix of the user id, so crafted ids cannot collide with or traverse
   into another user's directory.
3. ``guard_tool_call``   — PreToolUse inspection: every path-like argument
   must resolve (symlinks and ``..`` included) to a location inside the
   caller's workspace, otherwise the tool call is denied.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Keys that Claude built-in tools use to carry filesystem paths.
_PATH_KEYS = frozenset(
    {"file_path", "path", "notebook_path", "cwd", "directory", "dir"}
)

# Tools that operate purely on in-memory state and carry no filesystem paths.
_PATHLESS_TOOLS = frozenset({"TodoWrite"})


class IsolationError(ValueError):
    """Raised when a user id fails validation."""


def validate_user_id(user_id: object) -> str:
    """Return the canonical user id or raise ``IsolationError``."""
    if not isinstance(user_id, str) or not USER_ID_RE.match(user_id):
        raise IsolationError(
            "user_id must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
        )
    return user_id


def user_slug(user_id: str) -> str:
    """Directory-safe unique name: readable prefix + id hash.

    The hash suffix guarantees uniqueness even if two distinct raw ids
    normalise to the same readable prefix (e.g. ``a.b`` vs ``a_b``).
    """
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:12]
    readable = re.sub(r"[^A-Za-z0-9]+", "-", user_id).strip("-").lower()[:24]
    return f"{readable or 'user'}-{digest}"


def workspace_for(users_root: Path, user_id: str) -> Path:
    """Absolute per-user workspace path (not created here)."""
    return (users_root / user_slug(validate_user_id(user_id))).absolute()


def ensure_workspace(users_root: Path, user_id: str) -> Path:
    """Create (0700) and return the user's workspace directory."""
    ws = workspace_for(users_root, user_id)
    ws.mkdir(parents=True, exist_ok=True, mode=0o700)
    return ws


def _iter_path_candidates(tool_input: dict) -> list[str]:
    """Collect every string that the tool would treat as a filesystem path."""
    candidates: list[str] = []
    for key, value in tool_input.items():
        if key in _PATH_KEYS and isinstance(value, str) and value:
            candidates.append(value)
    return candidates


def _resolves_inside(candidate: str, workspace: Path) -> bool:
    """True iff ``candidate`` resolves strictly inside ``workspace``.

    ``Path.resolve()`` collapses ``..`` and follows existing symlinks, so a
    symlink planted inside the workspace pointing elsewhere is also caught.
    Relative paths are interpreted against the workspace (the Claude ``cwd``).
    ``~`` is expanded to the workspace itself, because each agent subprocess
    runs with ``HOME`` set to its workspace.
    """
    if candidate == "~" or candidate.startswith("~/"):
        candidate = str(workspace) + candidate[1:]
    elif candidate.startswith("~"):
        return False  # ~otheruser — never legitimate here
    path = Path(candidate)
    if not path.is_absolute():
        path = workspace / path
    resolved = path.resolve()
    ws = workspace.resolve()
    return resolved == ws or ws in resolved.parents


def guard_tool_call(
    tool_name: str, tool_input: dict, workspace: Path
) -> str | None:
    """Validate one tool call. Returns None if allowed, else a deny reason.

    Deny-by-default posture:
    - unknown path-like keys are covered by ``_PATH_KEYS``;
    - tools without any path argument are only allowed when they are known
      to be path-less (``TodoWrite``); ``Glob``/``Grep`` default their search
      root to ``cwd`` (the workspace) when no path is given, which is safe.
    """
    if tool_name in _PATHLESS_TOOLS:
        return None

    candidates = _iter_path_candidates(tool_input)
    for raw in candidates:
        if not _resolves_inside(raw, workspace):
            return (
                f"path '{raw}' resolves outside the per-user workspace; "
                "cross-user access is forbidden"
            )
    return None
