"""Static deployment-contract tests; no AWS calls are made."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent.parent
DEPLOY = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")


class TestDeployContract(unittest.TestCase):
    def test_create_and_update_use_public_microvm_network(self):
        flag = '--network-configuration \'{"networkMode":"PUBLIC"}\''
        self.assertEqual(DEPLOY.count(flag), 2)

    def test_deployment_does_not_supply_capacity_provider_or_filesystem(self):
        self.assertNotIn("--capacity-provider-configuration", DEPLOY)
        self.assertNotIn("--filesystem-configurations", DEPLOY)


if __name__ == "__main__":
    unittest.main()
