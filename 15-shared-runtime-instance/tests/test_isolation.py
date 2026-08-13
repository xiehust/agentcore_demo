"""Unit tests for app/isolation.py (standard library only)."""

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
    user_slug,
    validate_user_id,
    workspace_for,
)


class TestValidateUserId(unittest.TestCase):
    def test_accepts_normal_ids(self):
        for uid in ["alice", "bob-2", "user.name_01", "A" * 64]:
            self.assertEqual(validate_user_id(uid), uid)

    def test_rejects_bad_ids(self):
        bad = [
            "",
            None,
            123,
            "../etc",
            "a/b",
            "a b",
            "-leading-dash",
            ".hidden",
            "A" * 65,
            "中文",
            "a\x00b",
        ]
        for uid in bad:
            with self.assertRaises(IsolationError, msg=repr(uid)):
                validate_user_id(uid)


class TestWorkspaceDerivation(unittest.TestCase):
    def test_slug_unique_for_similar_ids(self):
        self.assertNotEqual(user_slug("a.b"), user_slug("a_b"))
        self.assertNotEqual(user_slug("alice"), user_slug("Alice"))

    def test_workspace_stays_under_root(self):
        root = Path("/data/users")
        ws = workspace_for(root, "alice")
        self.assertIn(root, ws.parents)

    def test_ensure_workspace_creates_private_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = ensure_workspace(Path(tmp), "alice")
            self.assertTrue(ws.is_dir())
            self.assertEqual(ws.stat().st_mode & 0o777, 0o700)


class TestGuardToolCall(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.alice = ensure_workspace(self.root, "alice")
        self.bob = ensure_workspace(self.root, "bob")

    def tearDown(self):
        self._tmp.cleanup()

    def test_allows_inside_paths(self):
        cases = [
            ("Write", {"file_path": str(self.alice / "notes.txt")}),
            ("Read", {"file_path": "notes.txt"}),                 # relative
            ("Read", {"file_path": "sub/dir/deep.txt"}),
            ("Glob", {"pattern": "**/*.py"}),                     # no path key
            ("TodoWrite", {"todos": []}),                          # path-less tool
        ]
        for tool, tool_input in cases:
            self.assertIsNone(
                guard_tool_call(tool, tool_input, self.alice), msg=(tool, tool_input)
            )

    def test_denies_absolute_escape(self):
        for target in ["/etc/passwd", str(self.bob / "secret.txt"), "/proc/1/environ"]:
            self.assertIsNotNone(
                guard_tool_call("Read", {"file_path": target}, self.alice),
                msg=target,
            )

    def test_denies_relative_traversal(self):
        bob_dir = self.bob.name
        for target in ["../x.txt", f"../{bob_dir}/secret.txt", "a/../../b"]:
            self.assertIsNotNone(
                guard_tool_call("Read", {"file_path": target}, self.alice),
                msg=target,
            )

    def test_denies_symlink_escape(self):
        (self.bob / "secret.txt").write_text("bob-token")
        link = self.alice / "innocent.txt"
        os.symlink(self.bob / "secret.txt", link)
        self.assertIsNotNone(
            guard_tool_call("Read", {"file_path": "innocent.txt"}, self.alice)
        )

    def test_denies_cwd_and_directory_keys(self):
        self.assertIsNotNone(
            guard_tool_call("LS", {"path": str(self.root)}, self.alice)
        )
        self.assertIsNotNone(
            guard_tool_call("Grep", {"path": "/"}, self.alice)
        )

    def test_tilde_paths(self):
        # ~ maps to the user's own workspace (HOME override) — allowed
        self.assertIsNone(
            guard_tool_call("Write", {"file_path": "~/notes.txt"}, self.alice)
        )
        # ~root / ~otheruser style — always denied
        self.assertIsNotNone(
            guard_tool_call("Write", {"file_path": "~root/x.txt"}, self.alice)
        )
        # ~ escaping via traversal still denied
        self.assertIsNotNone(
            guard_tool_call("Write", {"file_path": "~/../escape.txt"}, self.alice)
        )

    def test_workspace_root_itself_is_allowed(self):
        self.assertIsNone(guard_tool_call("LS", {"path": str(self.alice)}, self.alice))
        self.assertIsNone(guard_tool_call("LS", {"path": "."}, self.alice))


if __name__ == "__main__":
    unittest.main()
