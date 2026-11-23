import io
import json
import sys
import tarfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from prepare_joynext import INVENTORY, TRAIN, add_package, guest_path, selected


class PackageTests(unittest.TestCase):
    def test_pinned_inventory(self):
        modules = json.loads(INVENTORY.read_text())
        self.assertEqual(len({m["module"] for m in modules}), len(modules))
        self.assertEqual(len(modules), 111)
        for module in modules:
            self.assertTrue(selected(module["module"]))
            self.assertTrue(module["manifest"].startswith(f"Meta/{TRAIN}/"))
            self.assertRegex(module["sha256"], r"^[0-9a-f]{64}$")

    def test_selection(self):
        self.assertTrue(selected("emmc.hmi.pcc-hmi-vw-eu_high"))
        self.assertFalse(selected("emmc.hmi.pcc-hmi-vw-eu_low"))
        self.assertFalse(selected("emmc.system.swupdate"))

    def test_paths(self):
        self.assertEqual(guest_path("/lib/libc.so.6"), "lib/libc.so.6")
        for name in ("../../etc/passwd", "/x/../y", "/", "."):
            with self.assertRaises(ValueError):
                guest_path(name)

    def test_conflict_and_link_ancestor(self):
        blob = io.BytesIO()
        with tarfile.open(fileobj=blob, mode="w") as t:
            e = tarfile.TarInfo("/lib/a"); e.size = 3
            t.addfile(e, io.BytesIO(b"ELF"))
        for previous in ({"lib/a": {"type": "0", "sha256": "different"}},
                         {"lib": {"type": "2", "link": "/somewhere"}}):
            blob.seek(0)
            with tarfile.open(fileobj=blob) as src, tarfile.open(fileobj=io.BytesIO(), mode="w") as dst:
                with self.assertRaises(ValueError):
                    add_package(src, dst, "test", previous)


if __name__ == "__main__":
    unittest.main()
