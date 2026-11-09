"""Use stored POEDB passages to distinguish stealing modifiers from leech."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import unittest

from retrieval.hybrid import retrieve
from retrieval.terminology import search_query
from retrieval.tokenize import tokens_for_fts


class SearchTerminologyTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript(
            "CREATE TABLE alias(alias, alias_norm, entity_id, kind);"
            "CREATE TABLE chunk(chunk_id, entity_id, text, metadata);"
            "CREATE VIRTUAL TABLE chunk_fts USING fts5("
            "tokens, keywords, chunk_id UNINDEXED, entity_id UNINDEXED);"
        )
        fixtures = Path(__file__).parent / "fixtures" / "search_terminology.json"
        for row in json.loads(fixtures.read_text(encoding="utf-8")):
            self.conn.execute("INSERT INTO chunk VALUES (?,?,?,?)", tuple(row.values()))
            self.conn.execute("INSERT INTO chunk_fts VALUES (?,?,?,?)", (
                tokens_for_fts(row["text"]), "", row["chunk_id"], row["entity_id"],
            ))

    def test_full_phrase_retrieves_monster_modifiers_instead_of_the_mod_pool(self):
        for question in ("偷取词缀的腰带", "窃取怪物的词缀的腰带", "词缀偷取的腰带"):
            with self.subTest(question=question):
                result = retrieve(self.conn, question, top_k=8)
                self.assertEqual(question, result.query)
                self.assertTrue(result.sufficient)
                self.assertNotIn("mod_intent", result.channels)
                self.assertIn("unique_item:Headhunter", [h.entity_id for h in result.hits[:3]])
                self.assertIn("unique_item:Replica_Headhunter", [h.entity_id for h in result.hits])

    def test_item_class_is_not_replaced_by_a_hardcoded_entity(self):
        result = retrieve(self.conn, "偷取词缀的珠宝", top_k=8)
        self.assertTrue(result.sufficient)
        self.assertEqual("unique_item:Inspired_Learning", result.hits[0].entity_id)

    def test_leech_and_ordinary_affix_queries_keep_their_meaning(self):
        for question in ("生命偷取的腰带", "魔力偷取", "腰带生命词缀", "不偷取生命"):
            self.assertEqual(question, search_query(question))
        result = retrieve(self.conn, "生命偷取的腰带", top_k=8)
        self.assertIn(result.hits[0].entity_id, {"unique_item:Wurms_Molt", "unique_item:The_Retch"})
        self.assertIn("mod_intent", retrieve(self.conn, "腰带词缀", top_k=8).channels)

    def test_negation_and_compound_constraints_remain_in_search_text(self):
        self.assertEqual("不要获得怪物属性的腰带，要生命偷取的腰带",
                         search_query("不要偷取词缀的腰带，要生命偷取的腰带"))

    def test_no_matching_version_remains_a_gap(self):
        result = retrieve(self.conn, "偷取词缀的腰带", version="not-indexed")
        self.assertFalse(result.sufficient)
        self.assertEqual([], result.hits)
