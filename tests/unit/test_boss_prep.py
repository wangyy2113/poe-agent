"""Wiki 原句与 PoB 快照的条件性数值对照，不升级成整场通关结论。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.deps import require_user
from main import create_app
from parsers.pob_build import PobBuild, PobParseError
from planner.boss_prep import compare_boss_guide


GUIDE = {
    "boss_key": "sirus", "page_title": "Sirus, Awakener of Worlds",
    "source_url": "https://www.poewiki.net/wiki/Sirus,_Awakener_of_Worlds",
    "fetched_at": "2026-10-30T00:00:00Z", "content_hash": "abc123",
    "sections": [{"heading": "Recommendations", "lines": [
        "• At least 1 million damage per second",
        "• At least 6,000 combined Life and Energy Shield and a reliable source of recovery",
        "• Capped elemental resistances (fire, cold and lightning resistance at 75%); additional Maximum resistance is also useful.",
        "• At least 999 million damage per second from an expanded tooltip",  # 不符合完整句
    ]}, {"heading": "Drops", "lines": ["• At least 900 million damage per second"]}],
}


class BossPrepTest(unittest.TestCase):
    def test_sourced_checks_keep_assumptions_and_missing_fields(self) -> None:
        build = PobBuild(
            tree_version="3.29", config={"enemyIsBoss": "Pinnacle"},
            player_stats={
                "FullDPS": 1_200_000, "LifeUnreserved": 4_000, "EnergyShield": 1_500,
                "FireResist": 75, "ColdResist": 70, "LightningResist": 77,
            },
        )
        result = compare_boss_guide(GUIDE, build)
        self.assertEqual("partial", result["status"])
        self.assertEqual(["dps", "combined_life_es", "elemental_resistance"],
                         [item["metric"] for item in result["checks"]])
        dps, pool, resists = result["checks"]
        self.assertEqual("unknown", dps["status"])
        self.assertIsNone(dps["difference"])
        self.assertEqual(["FullDPS", "Config.enemyIsBoss"], dps["build_fields"])
        self.assertEqual("below", pool["status"])
        self.assertEqual(-500, pool["difference"])
        self.assertIn("回复", pool["unassessed_clause"])
        self.assertEqual("below", resists["status"])
        self.assertEqual(5, resists["difference"]["ColdResist"])
        self.assertEqual(0, resists["difference"]["FireResist"])
        self.assertTrue(all(item["source_url"].endswith("#Recommendations")
                            and item["source_hash"] == "abc123" for item in result["checks"]))
        self.assertIn("当前游戏版本", " ".join(result["missing"]))

        confirmed = compare_boss_guide(GUIDE, build, same_boss_configuration=True)
        self.assertEqual("at_or_above", confirmed["checks"][0]["status"])
        self.assertEqual(200_000, confirmed["checks"][0]["difference"])
        self.assertFalse(any("通关保证" in str(item) for item in confirmed["checks"]))

    def test_missing_stats_are_not_zero_and_non_boss_config_cannot_compare_dps(self) -> None:
        build = PobBuild(player_stats={"FullDPS": 1_500_000, "Life": 5000,
                                         "FireResist": 75, "ColdResist": 75})
        result = compare_boss_guide(GUIDE, build, same_boss_configuration=True)
        self.assertEqual(["unknown", "unknown", "unknown"],
                         [item["status"] for item in result["checks"]])
        self.assertIsNone(result["checks"][1]["build_value"])
        self.assertIsNone(result["checks"][2]["build_value"])

    def test_unstructured_page_does_not_invent_threshold(self) -> None:
        guide = {**GUIDE, "sections": [
            {"heading": "Strategy", "lines": [
                "At least 1 million damage per second", "Use a flask in this fight.",
            ]},
            {"heading": "Recommendations", "lines": [
                "• Capped elemental resistances (fire, cold and lightning resistance at 75%);"
                + " expanded tooltip " * 50,
            ]},
        ]}
        result = compare_boss_guide(guide, PobBuild())
        self.assertEqual("unknown", result["status"])
        self.assertEqual([], result["checks"])
        self.assertIn("药剂", " ".join(result["missing"]))

    def test_api_requires_user_and_handles_missing_guide_and_invalid_code(self) -> None:
        app = create_app()
        path = "/poe_agent/boss/guides/sirus/build-check"
        with patch("api.deps.user_keys", return_value=["test-key"]):
            with TestClient(app) as client:
                self.assertIn(client.post(path, json={"code": "valid"}).status_code, (401, 403))
        app.dependency_overrides[require_user] = lambda: "test"
        with patch("api.boss_guides.dbmod.get_conn", return_value=object()), patch(
            "api.boss_guides.boss_guides.detail", return_value=GUIDE
        ), patch("api.boss_guides.decode_share_code", return_value="<Build/>") as decode, patch(
            "api.boss_guides.parse_build",
            return_value=PobBuild(config={"enemyIsBoss": "Pinnacle"},
                                  player_stats={"FullDPS": 1_100_000}),
        ):
            with TestClient(app) as client:
                good = client.post(path, json={"code": "valid", "same_boss_configuration": True})
                self.assertEqual(200, good.status_code)
                self.assertEqual("at_or_above", good.json()["checks"][0]["status"])
                self.assertEqual("partial", good.json()["status"])
                self.assertEqual(1, decode.call_count)
                self.assertEqual(404, client.post(
                    "/poe_agent/boss/guides/unknown/build-check", json={"code": "valid"}
                ).status_code)
        with patch("api.boss_guides.dbmod.get_conn", return_value=object()), patch(
            "api.boss_guides.boss_guides.detail", return_value=GUIDE
        ), patch("api.boss_guides.decode_share_code", side_effect=PobParseError("invalid")):
            with TestClient(app) as client:
                self.assertEqual(422, client.post(path, json={"code": "broken"}).status_code)


if __name__ == "__main__":
    unittest.main()
