"""Unit tests for app/isolation.py."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from isolation import (  # noqa: E402
    IsolationError,
    ensure_workspace,
    guard_tool_call,
    resolve_user_id,
    user_slug,
    validate_user_id,
    workspace_for,
)


class TestValidateUserId(unittest.TestCase):
    def test_accepts_allowlisted_ids(self):
        for user_id in ("alice", "bob-2", "user.name_01", "A" * 64):
            self.assertEqual(validate_user_id(user_id), user_id)

    def test_transport_and_payload_identity_must_match(self):
        self.assertEqual(resolve_user_id("alice", "alice"), "alice")
        self.assertEqual(resolve_user_id(None, "alice"), "alice")
        self.assertEqual(resolve_user_id("alice", None), "alice")
        with self.assertRaises(IsolationError):
            resolve_user_id("alice", "bob")

    def test_rejects_invalid_ids(self):
        for user_id in (
            "",
            None,
            123,
            "../etc",
            "a/b",
            "a b",
            "-leading",
            ".hidden",
            "A" * 65,
            "中文",
            "a\x00b",
        ):
            with self.assertRaises(IsolationError, msg=repr(user_id)):
                validate_user_id(user_id)


class TestWorkspaceDerivation(unittest.TestCase):
    def test_similar_ids_have_distinct_slugs(self):
        self.assertNotEqual(user_slug("a.b"), user_slug("a_b"))
        self.assertNotEqual(user_slug("alice"), user_slug("Alice"))

    def test_workspace_stays_below_root(self):
        root = Path("/tmp/example-users")
        self.assertIn(root, workspace_for(root, "alice").parents)

    def test_workspace_permissions_are_repaired(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = ensure_workspace(Path(temporary), "alice")
            workspace.chmod(0o755)
            workspace = ensure_workspace(Path(temporary), "alice")
            self.assertEqual(workspace.stat().st_mode & 0o777, 0o700)

    def test_workspace_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            workspace_for(root, "alice").symlink_to(target, target_is_directory=True)
            with self.assertRaises(IsolationError):
                ensure_workspace(root, "alice")


class TestPathGuard(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.alice = ensure_workspace(self.root, "alice")
        self.bob = ensure_workspace(self.root, "bob")

    def tearDown(self):
        self.temporary.cleanup()

    def test_allows_workspace_paths(self):
        cases = (
            ("Write", {"file_path": str(self.alice / "notes.txt")}),
            ("Read", {"file_path": "notes.txt"}),
            ("Read", {"file_path": "sub/deep.txt"}),
            ("Glob", {"pattern": "**/*.py"}),
            ("TodoWrite", {"todos": []}),
            ("LS", {"path": "."}),
        )
        for tool, value in cases:
            self.assertIsNone(guard_tool_call(tool, value, self.alice))

    def test_denies_absolute_and_relative_escape(self):
        targets = (
            "/etc/passwd",
            str(self.bob / "secret.txt"),
            "/proc/1/environ",
            "../x.txt",
            f"../{self.bob.name}/secret.txt",
            "a/../../b",
        )
        for target in targets:
            self.assertIsNotNone(
                guard_tool_call("Read", {"file_path": target}, self.alice),
                msg=target,
            )

    def test_denies_symlink_escape(self):
        secret = self.bob / "secret.txt"
        secret.write_text("bob-token")
        os.symlink(secret, self.alice / "innocent.txt")
        self.assertIsNotNone(
            guard_tool_call("Read", {"file_path": "innocent.txt"}, self.alice)
        )
        os.symlink(self.bob, self.alice / "linked-dir")
        self.assertIsNotNone(
            guard_tool_call("Glob", {"pattern": "linked-dir/**/*.txt"}, self.alice)
        )

    def test_glob_pattern_is_a_guarded_path(self):
        self.assertIsNone(guard_tool_call("Glob", {"pattern": "**/*.py"}, self.alice))
        for pattern in ("../*", "/tmp/**/*", f"../{self.bob.name}/**/*"):
            self.assertIsNotNone(
                guard_tool_call("Glob", {"pattern": pattern}, self.alice),
                msg=pattern,
            )

    def test_malformed_tool_input_is_denied(self):
        self.assertIsNotNone(guard_tool_call("Read", None, self.alice))

    def test_tilde_handling(self):
        self.assertIsNone(
            guard_tool_call("Write", {"file_path": "~/notes.txt"}, self.alice)
        )
        self.assertIsNotNone(
            guard_tool_call("Write", {"file_path": "~root/x"}, self.alice)
        )
        self.assertIsNotNone(
            guard_tool_call("Write", {"file_path": "~/../escape"}, self.alice)
        )


if __name__ == "__main__":
    unittest.main()
