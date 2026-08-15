"""Local tests for the command-side long-run artifact verifier program."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from load_test_longrun import _ARTIFACT_VERIFIER, EXPECTED_FILES  # noqa: E402


class TestArtifactVerifier(unittest.TestCase):
    def setUp(self):
        self.users_root = Path("/tmp/agentcore-users")
        self.users_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(
            prefix="verifier-test-", dir=self.users_root
        )
        self.workspace = Path(self.temporary.name)
        self.project = self.workspace / "webapp"
        self.project.mkdir()
        self.token = "unit-test-token-123"
        self._write_valid_project()

    def tearDown(self):
        self.temporary.cleanup()

    def _write_valid_project(self):
        filler = " useful content" * 60
        (self.project / "index.html").write_text(
            "<html><head><link href='styles.css'></head><body>"
            "Shared Runtime Project Board <a href='about.html'>About</a>"
            "<script src='app.js'></script>"
            + filler
            + "<footer>v2.0</footer></body></html>"
        )
        (self.project / "about.html").write_text(
            "<html><head><link href='styles.css'></head><body>"
            "<a href='index.html'>Board</a>"
            + filler
            + "<footer>v2.0</footer></body></html>"
        )
        (self.project / "styles.css").write_text(
            "@media (max-width: 480px){} @media (min-width: 768px){}" + filler
        )
        (self.project / "app.js").write_text(
            "const store = localStorage; const node = document.createElement('p');"
            "node.textContent = 'safe';" + filler
        )
        (self.project / "README.md").write_text(self.token + filler)
        (self.project / "loadtest.json").write_text(
            json.dumps(
                {
                    "run_token": self.token,
                    "status": "complete",
                    "expected_files": list(EXPECTED_FILES),
                },
                indent=2,
            )
            + "\n"
        )

    def _verify(self, workspace: Path | None = None):
        verifier = self.workspace / "verify.py"
        specs = self.workspace / "specs.json"
        verifier.write_text(_ARTIFACT_VERIFIER)
        specs.write_text(
            json.dumps(
                [
                    {
                        "user_id": "unit-user",
                        "workspace": str(workspace or self.workspace),
                        "run_token": self.token,
                    }
                ]
            )
        )
        completed = subprocess.run(
            [sys.executable, str(verifier), str(specs)],
            check=True,
            text=True,
            capture_output=True,
        )
        return json.loads(completed.stdout)["unit-user"]

    def test_accepts_exact_valid_project(self):
        result = self._verify()
        self.assertTrue(result["success"], result["errors"])
        self.assertEqual(set(result["file_sizes"]), set(EXPECTED_FILES))

    def test_rejects_workspace_symlink(self):
        alias = self.users_root / f"{self.workspace.name}-link"
        alias.symlink_to(self.workspace, target_is_directory=True)
        try:
            result = self._verify(alias)
        finally:
            alias.unlink()
        self.assertFalse(result["success"])
        self.assertIn("workspace cannot be a symlink", result["errors"])

    def test_rejects_manifest_order_and_malformed_values(self):
        manifest_path = self.project / "loadtest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["expected_files"] = list(reversed(EXPECTED_FILES))
        manifest_path.write_text(json.dumps(manifest) + "\n")
        result = self._verify()
        self.assertFalse(result["success"])
        self.assertTrue(
            any("expected_files mismatch" in error for error in result["errors"])
        )

        manifest["expected_files"] = [{}]
        manifest_path.write_text(json.dumps(manifest) + "\n")
        result = self._verify()
        self.assertFalse(result["success"])
        self.assertTrue(
            any("expected_files mismatch" in error for error in result["errors"])
        )

    def test_rejects_extra_file_and_unsafe_dom_write(self):
        (self.project / "extra.txt").write_text("unexpected")
        with (self.project / "app.js").open("a") as handle:
            handle.write("\ndocument.body.innerHTML = 'unsafe';")
        result = self._verify()
        self.assertFalse(result["success"])
        self.assertTrue(any("entries mismatch" in error for error in result["errors"]))
        self.assertTrue(any("innerHTML" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
