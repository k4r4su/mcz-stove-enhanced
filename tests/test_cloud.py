import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import mczcloud


class MaskTests(unittest.TestCase):
    def test_masks_personal_keys_recursively(self):
        data = {"Node": {"Id": "abc", "Latitude": 48.1, "Name": "Poele", "Nested": [{"Email": "a@b.c", "Value": 3}]}}
        masked = mczcloud.mask(data)
        self.assertEqual(masked["Node"]["Latitude"], "***")
        self.assertEqual(masked["Node"]["Nested"][0]["Email"], "***")
        self.assertEqual(masked["Node"]["Id"], "abc")
        self.assertEqual(masked["Node"]["Nested"][0]["Value"], 3)


if __name__ == "__main__":
    unittest.main()
