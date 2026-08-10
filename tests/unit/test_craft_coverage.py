"""制作可行性覆盖声明的实时一致性测试。"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kb.coverage import DOMAINS, load  # noqa: E402
from tests.unit.test_item_mods import fixture_conn  # noqa: E402


class TestCraftCoverage(unittest.TestCase):
    def test_旧快照缺少新增领域时回退实时探测(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE kb_coverage(domain_key TEXT)")
        conn.execute("INSERT INTO kb_coverage VALUES ('unique_item')")

        statuses = load(conn)

        self.assertEqual({status.key for status in statuses}, {domain.key for domain in DOMAINS})
        self.assertIn("craft_feasibility", {status.key for status in statuses})

    def test_family计数排除空数组(self):
        conn = fixture_conn()
        self.addCleanup(conn.close)
        conn.execute(
            "UPDATE mod_affix SET family_list='[]' "
            "WHERE uid=(SELECT uid FROM mod_affix ORDER BY uid LIMIT 1)"
        )
        spec = next(domain for domain in DOMAINS if domain.key == "craft_feasibility")

        counted = conn.execute(spec.count_sql).fetchone()[0]
        expected = conn.execute(
            "SELECT count(*) FROM mod_affix "
            "WHERE json_valid(family_list) AND json_array_length(family_list)>0"
        ).fetchone()[0]
        total = conn.execute("SELECT count(*) FROM mod_affix").fetchone()[0]

        self.assertEqual(counted, expected)
        self.assertLess(counted, total)


if __name__ == "__main__":
    unittest.main(verbosity=2)
