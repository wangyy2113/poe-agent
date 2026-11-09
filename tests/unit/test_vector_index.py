"""Legacy vector diagnostics are read-only and require no extension."""
import sqlite3
import unittest
from retrieval.vector import vector_index_metadata


class LegacyIndexTest(unittest.TestCase):
    def test_empty_and_existing_metadata_are_inspected_without_mutation(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        self.assertFalse(vector_index_metadata(conn)["present"])
        conn.execute("CREATE TABLE vector_index_state(id INTEGER,dim INTEGER,indexed_count INTEGER,dirty INTEGER)")
        conn.execute("INSERT INTO vector_index_state VALUES (1,768,5,1)")
        before = conn.total_changes
        self.assertEqual(vector_index_metadata(conn), {
            "present": True, "dimension": 768, "indexed_count": 5, "dirty": True,
        })
        self.assertEqual(before, conn.total_changes)
