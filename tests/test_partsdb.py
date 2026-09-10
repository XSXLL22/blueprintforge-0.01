"""器件库自身的健全性检查：示例 IR 引用的料必须存在，数据字段合法。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from parts.partsdb import CATEGORIES, PARTS

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "buck_12v_to_3v3.json"
LIFECYCLES = ("active", "nrfnd", "eol")


class TestPartsDb(unittest.TestCase):
    def test_example_parts_all_exist(self):
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        for c in raw["components"]:
            self.assertIn(c["part"], PARTS,
                          f"{c['ref']}: {c['part']} 不在器件库中")

    def test_fields_legal(self):
        for name, p in PARTS.items():
            self.assertIn(p.category, CATEGORIES, name)
            self.assertIn(p.lifecycle, LIFECYCLES, name)
            self.assertTrue(p.package, name)
            self.assertTrue(p.spec.strip(), name)
            if p.price_cny is not None:
                self.assertGreaterEqual(p.price_cny, 0, name)
            if p.stock is not None:
                self.assertGreaterEqual(p.stock, 0, name)


if __name__ == "__main__":
    unittest.main()
