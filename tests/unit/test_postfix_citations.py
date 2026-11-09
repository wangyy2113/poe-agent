"""Explicit, same-line postfix citations belong only to the preceding sentence."""
from __future__ import annotations

import unittest

from orchestrator.binding import Evidence, StreamingGate, split_sentences, verify


class PostfixCitationsTest(unittest.TestCase):
    def setUp(self):
        self.evidence = [Evidence(label="E1", chunk_id="c1", entity_id=None,
            title="属性", url="https://example.test", source="POEDB", source_priority=1,
            text="+(40-55) 力量；+(40-55) 敏捷", can_support_facts=True)]

    def check_parity(self, text):
        batch = verify(text, self.evidence)
        self.assertEqual("".join(split_sentences(text)), text)
        for size in range(1, len(text) + 1):
            gate = StreamingGate(self.evidence)
            for pos in range(0, len(text), size):
                gate.feed(text[pos:pos + size])
            gate.flush()
            self.assertEqual("".join(gate.result().text.split()), "".join(batch.text.split()), (size, text))
            self.assertEqual(gate.result().blocked, batch.blocked, (size, text))
        return batch

    def test_postfix_citation_same_line_at_every_stream_boundary(self):
        for text in ("力量 +40。[[E1]]", "力量 +40。 [[E1]]\n",
                     "- 力量 +40。[[E1]]\n- 敏捷 +40。[[E1]]",
                     "力量 +40。[E1]敏捷 +40。[[E1]]",
                     "力量 +40。[[E1]]\n\n敏捷 +40[[E1]]。"):
            with self.subTest(text=text):
                self.assertEqual(self.check_parity(text).blocked, 0)

    def test_bare_punctuation_waits_for_citation(self):
        gate = StreamingGate(self.evidence)
        self.assertEqual(gate.feed("力量 +40。"), [])
        self.assertEqual(gate.feed("["), [])
        self.assertEqual(gate.feed("[E1"), [])
        gate.feed("]]\n")
        gate.flush()
        self.assertEqual(gate.result().blocked, 0)

    def test_no_inheritance_to_prior_or_next_sentence(self):
        self.assertEqual(self.check_parity("它让你免疫冰冻。力量 +40。[[E1]]").blocked, 1)
        self.assertEqual(self.check_parity("力量 +40。[[E1]]它让你免疫冰冻。").blocked, 1)

    def test_no_cross_line_or_cross_paragraph_attachment(self):
        for text in ("力量 +40。\n[[E1]]", "力量 +40。\n\n[[E1]]", "力量 +40。\r\n[[E1]]"):
            with self.subTest(text=text):
                self.assertEqual(self.check_parity(text).blocked, 1)

    def test_invalid_numbers_and_unknown_citations_are_still_blocked(self):
        self.assertEqual(self.check_parity("力量 +999。[[E1]]").blocked, 1)
        self.assertEqual(self.check_parity("力量 +40。[[E99]]").blocked, 1)
