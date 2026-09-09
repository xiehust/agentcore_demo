"""Offline lifecycle safety tests: no AWS requests or deletion."""
import argparse
import unittest
from unittest.mock import MagicMock, patch

import lab


class CleanupTests(unittest.TestCase):
    def run_partial(self, state):
        aws = MagicMock()
        aws.get_caller_identity.return_value = {"Account": "test-account"}
        aws.describe_network_interfaces.return_value = {"NetworkInterfaces": []}
        with patch.object(lab, "S", state), patch.object(lab, "args", argparse.Namespace(confirm=True), create=True), patch.object(lab, "client", return_value=aws), patch.object(lab, "save"), patch("builtins.print"):
            lab.cleanup()
        return aws

    def test_empty_creation_state(self):
        aws = self.run_partial({"account": "test-account"})
        aws.delete_subnet.assert_not_called()
        aws.delete_security_group.assert_not_called()
        aws.terminate_instances.assert_not_called()

    def test_only_subnet_created(self):
        aws = self.run_partial({"account": "test-account", "subnet": "subnet-lab"})
        aws.delete_subnet.assert_called_once_with(SubnetId="subnet-lab")
        aws.delete_security_group.assert_not_called()

    def test_unconfirmed_cleanup_never_contacts_aws(self):
        with patch.object(lab, "args", argparse.Namespace(confirm=False), create=True), patch.object(lab, "client") as client:
            with self.assertRaises(SystemExit):
                lab.cleanup()
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
