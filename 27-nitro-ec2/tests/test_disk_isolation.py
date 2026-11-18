"""Offline disk assignment/identity tests; no Nitro or filesystem formatting."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("disk_isolation", ROOT / "disk_isolation.py")
disk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(disk)

class DiskTests(unittest.TestCase):
    def test_existing_image_is_never_formatted(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "A.ext4"
            image.write_bytes(b"existing data")
            with patch.object(disk.common, "command") as command, self.assertRaises(FileExistsError):
                disk.create_disk(image)
            command.assert_not_called()
            self.assertEqual(image.read_bytes(), b"existing data")

    def test_duplicate_backend_and_hardlink_are_rejected(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "A", Path(tmp) / "B"
            a.write_bytes(b"A")
            b.write_bytes(b"B")
            disk.validate_backends([a, b])
            with self.assertRaises(ValueError):
                disk.validate_backends([a, a])
            link = Path(tmp) / "alias"
            os.link(a, link)
            with self.assertRaises(ValueError):
                disk.validate_backends([a, link])

    def test_data_drive_config_and_distinct_networks(self):
        args = disk.common.parser().parse_args(["--direct-mode"])
        a = disk.vm_config(args, "A", "run-a", Path("A.ext4"),
                           {"tap": "tap-a", "guest": "198.51.100.2", "host": "198.51.100.1"})
        b = disk.vm_config(args, "B", "run-b", Path("B.ext4"),
                           {"tap": "tap-b", "guest": "203.0.113.2", "host": "203.0.113.1"})
        self.assertNotEqual(a["drives"][0]["path_on_host"], b["drives"][0]["path_on_host"])
        self.assertEqual(a["drives"][0]["cache_type"], "Writeback")
        self.assertFalse(a["drives"][0]["is_root_device"])
        self.assertEqual(a["machine-config"]["mem_size_mib"], 2048)
        self.assertTrue(a["enclave"]["direct_mode"])
        self.assertIn("guest_ip=203.0.113.2", b["boot-source"]["boot_args"])

    def test_ambiguous_write_is_not_retried(self):
        from types import SimpleNamespace
        vm = disk.VM(SimpleNamespace(out=Path("unused")), "A", "initial", Path("A.ext4"),
                     {"guest": "198.51.100.2"})
        vm.identity = {"boot_id": "boot"}
        with patch.object(disk.common, "request", side_effect=TimeoutError()) as request, self.assertRaises(TimeoutError):
            vm.call("/write", scope="workspace", file="shared.txt", text="A")
        request.assert_called_once()

    def test_partial_start_is_terminated_without_health(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "A.ext4"
            image.write_bytes(b"fixture")
            vm = disk.VM(SimpleNamespace(out=Path(tmp)), "A", "initial", image, {"guest": "198.51.100.2"})
            vm.directory.mkdir()
            vm.proc = Mock()
            with patch.object(disk.common, "stop") as stop:
                vm.close()
            stop.assert_called_once()
            self.assertIsNone(vm.proc)

    def test_operation_cannot_accept_other_guest_identity(self):
        from types import SimpleNamespace
        vm = disk.VM(SimpleNamespace(out=Path("unused")), "A", "initial", Path("A.ext4"),
                     {"guest": "198.51.100.2"})
        vm.identity = {"boot_id": "boot"}
        with patch.object(disk.common, "request", return_value={"run_id": "other", "boot_id": "boot"}), \
                self.assertRaises(RuntimeError):
            vm.call("/read", scope="workspace", file="shared.txt")


if __name__ == "__main__":
    unittest.main()
