"""Native dependency selection for AL2023's glibc architecture."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("native_addons", Path(__file__).resolve().parents[1] / "scripts/native_addons.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class NativeTests(unittest.TestCase):
    def test_foreign_variants_are_not_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm = b"\x7fELF\x02\x01" + bytes(12) + (183).to_bytes(2, "little")
            x64 = b"\x7fELF\x02\x01" + bytes(12) + (62).to_bytes(2, "little")
            for name, header, expected in (("linux_arm64/koffi.node", arm, True),
                    ("musl_arm64/koffi.node", arm, False), ("bin/musl/system.node", arm, False),
                    ("linux-x64/pty.node", x64, False), ("darwin-arm64/pty.node", b"Mach-O", False),
                    ("win32-arm64/conpty.node", b"MZ", False)):
                file = root / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(header)
                self.assertEqual(native.compatible(file, "aarch64"), expected, name)


if __name__ == "__main__":
    unittest.main()
