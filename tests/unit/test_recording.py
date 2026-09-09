"""黄金录制格式的向后兼容与证据字段保真。"""

from __future__ import annotations

import unittest

from orchestrator.binding import Evidence
from tests.golden.recording import Recording


class RecordingEvidenceTest(unittest.TestCase):
    def test_派生通道往返保真且旧录制安全回退(self) -> None:
        evidence = Evidence(
            label="E1",
            chunk_id="calc:one",
            entity_id=None,
            title="计算结果",
            url="https://example.invalid/calc",
            source="Path of Building",
            source_priority=2,
            text="计算结果 79%",
            claim_scope="affix_pool_odds",
            claim_data={"kind": "pool_odds"},
            channel="derived",
        )
        payload = Recording(
            case_id="channel_round_trip",
            query="测试",
            raw_answer="因此结果为 79%[[E1]]。",
            evidence=[evidence],
        ).to_dict()

        self.assertEqual(payload["evidence"][0]["channel"], "derived")
        restored = Recording.from_dict(payload).evidence[0]
        self.assertEqual(restored.channel, "derived")
        self.assertEqual(restored.claim_scope, "affix_pool_odds")

        # v1 历史录制没有 channel；它们仍可回放，但不能被误标成计算器证据。
        legacy = dict(payload)
        legacy["evidence"] = [dict(payload["evidence"][0])]
        legacy["evidence"][0].pop("channel")
        self.assertEqual(Recording.from_dict(legacy).evidence[0].channel, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
