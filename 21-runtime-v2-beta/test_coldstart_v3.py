"""Offline regression checks for the fresh public-SDK matrix adapter."""
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile
import hashlib

import coldstart_v3 as ga


class PublicSDKTests(unittest.TestCase):
    def test_all_cells_are_fresh(self):
        out = ga.HERE / "results/not-created-v3"
        cells = ga.cell_plan(out)
        self.assertEqual(len(cells), 15)
        self.assertEqual(sum(c["concurrency"] for c in cells.values()), 1083)
        self.assertFalse(any(c["reused"] for c in cells.values()))
        self.assertTrue(all((ga.HERE / c["folder"]).parent == out for c in cells.values()))
        # Importing the v3 adapter must not alter the historical entry point.
        self.assertEqual(sum(c["reused"] for c in ga.matrix.cell_plan(out).values()), 3)

    def test_public_model_provenance_and_override_rejection(self):
        wheel = io.BytesIO()
        with zipfile.ZipFile(wheel, "w") as archive:
            for service in ("bedrock-agentcore", "bedrock-agentcore-control"):
                archive.writestr(f"botocore/data/{service}/2024-02-28/service-2.json", '{"shapes": {}}')
        data = wheel.getvalue()
        latest = json.dumps({"info": {"version": ga.boto3.__version__}}).encode()
        package = json.dumps({"urls": [{"filename": "botocore.whl", "url": "https://example.invalid/wheel",
                    "digests": {"sha256": hashlib.sha256(data).hexdigest()}}]}).encode()
        for changed in (False, True):
            with self.subTest(changed=changed), patch.object(ga.urllib.request, "urlopen",
                    side_effect=[io.BytesIO(latest), io.BytesIO(package), io.BytesIO(data)]), patch.object(ga, "Loader") as loader:
                loader.return_value.search_paths = ["public-models"]
                loader.return_value.load_service_model.return_value = {"shapes": {"private": {}}} if changed else {"shapes": {}}
                if changed:
                    with self.assertRaises(AssertionError):
                        ga.public_sdk_evidence()
                else:
                    evidence = ga.public_sdk_evidence()
                    self.assertEqual(len(evidence["models"]), 2)
                    self.assertTrue(all(m["matches_public_wheel"] for m in evidence["models"].values()))

    def test_reject_outdated_sdk_before_cloud_calls(self):
        with patch.object(ga.urllib.request, "urlopen", return_value=io.BytesIO(
                b'{"info": {"version": "0.0.0"}}')):
            with self.assertRaisesRegex(RuntimeError, "Upgrade boto3"):
                ga.public_sdk_evidence()


if __name__ == "__main__":
    unittest.main()
