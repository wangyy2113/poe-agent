"""GGG 官方角色 API 导入：固定端点、OAuth 边界与部分 Build 映射。"""

from __future__ import annotations

import unittest
import sqlite3
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from api.build import BuildRequest, analyze_build
from core import db as dbmod
from entities.taxonomy import ModKind
from parsers.character_api import (
    CharacterApiClient,
    CharacterApiError,
    character_build,
)


def _payload(*, realm: str = "pc") -> dict:
    return {
        "character": {
            "id": "a" * 64,
            "name": "火花测试号",
            "realm": realm,
            "class": "Elementalist",
            "league": "Keepers",
            "level": 95,
            "metadata": {"version": "3.27.0"},
            "equipment": [
                {
                    "inventoryId": "Helm",
                    "name": "Doom Crown",
                    "typeLine": "Hubris Circlet",
                    "baseType": "Hubris Circlet",
                    "rarity": "Rare",
                    "identified": True,
                    "ilvl": 84,
                    "influences": {"shaper": True},
                    "searing": True,
                    "properties": [
                        {"name": "Energy Shield", "values": [["210", 0]]},
                        {"name": "Quality", "values": [["+20%", 1]]},
                    ],
                    "requirements": [
                        {"name": "Level", "values": [["69", 0]]},
                        {"name": "Int", "values": [["154", 0]]},
                    ],
                    "sockets": [
                        {"group": 0, "sColour": "B"},
                        {"group": 0, "sColour": "B"},
                    ],
                    "socketedItems": [
                        {
                            "socket": 1,
                            "typeLine": "Added Lightning Damage Support",
                            "support": True,
                            "properties": [
                                {"name": "Level", "values": [["20", 0]]},
                            ],
                        },
                        {
                            "socket": 0,
                            "typeLine": "Arc",
                            "properties": [
                                {"name": "Level", "values": [["21", 0]]},
                                {"name": "Quality", "values": [["20%", 0]]},
                            ],
                        },
                    ],
                    "implicitMods": [{"description": "+20 to maximum Energy Shield"}],
                    "explicitMods": [
                        {"description": "+80 to maximum Life"},
                        {
                            "description": "+30% to Fire Resistance",
                            "flags": {"crafted": True},
                        },
                    ],
                }
            ],
            "inventory": [{"inventoryId": "MainInventory", "baseType": "Chaos Orb"}],
            "jewels": [
                {
                    "x": 123,
                    "name": "",
                    "baseType": "Cobalt Jewel",
                    "rarity": "Rare",
                    "identified": True,
                    "ilvl": 83,
                    "explicitMods": ["+7% to maximum Life"],
                }
            ],
            "passives": {
                "hashes": [100, 200],
                "hashes_ex": [70001],
                "mastery_effects": {"200": 55},
                "bandit_choice": "Eramir",
                "pantheon_major": "Lunaris",
                "pantheon_minor": "Shakari",
                "alternate_ascendancy": "Nameless Bloodline",
            },
        }
    }


class CharacterPayloadTest(unittest.TestCase):
    def test_官方响应转换成部分_build(self) -> None:
        build, meta = character_build(_payload())

        self.assertEqual(build.level, 95)
        self.assertEqual(build.class_name, "Witch")
        self.assertEqual(build.ascend_class_name, "Elementalist")
        self.assertEqual(build.tree_version, "3.27")
        self.assertIn("metadata.version", build.tree_version_basis)
        self.assertEqual(build.passive_nodes, ("100", "200", "70001"))
        self.assertEqual(build.tree_jewels, 1)
        self.assertEqual(build.bandit, "Eramir")
        self.assertEqual(build.pantheon_major, "Lunaris")
        self.assertEqual(meta.mastery_effects, (("200", "55"),))
        self.assertEqual(meta.alternate_ascendancy, "Nameless Bloodline")
        self.assertEqual(build.player_stats, {})
        self.assertTrue(any("PlayerStat" in warning for warning in build.warnings))

    def test_只纳入已装备物品和天赋珠宝(self) -> None:
        build, _meta = character_build(_payload())
        self.assertEqual(len(build.items), 2)
        helm, jewel = build.items
        self.assertEqual(helm.slot, "Helm")
        self.assertEqual(helm.parsed.base_type, "Hubris Circlet")
        self.assertEqual(helm.parsed.properties["energy_shield"], "210")
        self.assertEqual(helm.parsed.quality, 20)
        self.assertEqual(helm.parsed.requirements.intelligence, 154)
        self.assertEqual(helm.parsed.explicits[1].kind, ModKind.CRAFTED)
        self.assertEqual(helm.parsed.flags.influences, ["shaper", "searing_exarch"])
        self.assertTrue(jewel.slot.startswith("Jewel "))
        self.assertNotIn("Chaos Orb", [item.parsed.base_type for item in build.items])

    def test_按插槽连接组还原技能且不猜主技能(self) -> None:
        build, _meta = character_build(_payload())
        self.assertEqual(len(build.skill_groups), 1)
        group = build.skill_groups[0]
        self.assertEqual(group.slot, "Helm · 连接组 1")
        self.assertFalse(group.is_main)
        self.assertEqual([gem.name_en for gem in group.gems], [
            "Arc", "Added Lightning Damage Support",
        ])
        self.assertEqual(group.gems[0].level, 21)
        self.assertEqual(group.gems[0].quality, 20)
        self.assertTrue(group.gems[1].is_support)

    def test_拒绝_poe2_或缺少_character_对象(self) -> None:
        with self.assertRaisesRegex(CharacterApiError, "只支持 PoE1"):
            character_build(_payload(realm="poe2"))
        with self.assertRaisesRegex(CharacterApiError, "缺少 character"):
            character_build({"characters": []})
        with self.assertRaises(CharacterApiError) as missing:
            character_build({"character": None})
        self.assertEqual(missing.exception.code, "character_not_found")
        self.assertEqual(missing.exception.status_code, 404)


class CharacterApiClientTest(unittest.TestCase):
    def test_角色名只进入固定官方路径且_token_只进请求头(self) -> None:
        response = httpx.Response(
            200,
            json=_payload(),
            request=httpx.Request("GET", "https://api.pathofexile.com/character/x"),
        )
        with patch("parsers.character_api.httpx.get", return_value=response) as get:
            payload = CharacterApiClient(
                "secret-token", "OAuth test-client/1.0 (contact: test@example.com)"
            ).get_character("A/B")
        self.assertEqual(payload["character"]["name"], "火花测试号")
        args, kwargs = get.call_args
        self.assertEqual(args[0], "https://api.pathofexile.com/character/A%2FB")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret-token")
        self.assertNotIn("secret-token", args[0])
        self.assertFalse(kwargs["follow_redirects"])

    def test_未配置_token_和上游鉴权失败分开报错(self) -> None:
        with self.assertRaises(CharacterApiError) as missing:
            CharacterApiClient("", "agent").get_character("Alice")
        self.assertEqual(missing.exception.code, "character_api_not_configured")
        self.assertEqual(missing.exception.status_code, 503)

        with self.assertRaises(CharacterApiError) as bad_agent:
            CharacterApiClient("token", "poe1-agent/test").get_character("Alice")
        self.assertEqual(bad_agent.exception.code, "character_api_not_configured")
        self.assertIn("POE_OAUTH_USER_AGENT", bad_agent.exception.reason)

        response = httpx.Response(
            403,
            request=httpx.Request("GET", "https://api.pathofexile.com/character/Alice"),
        )
        with patch("parsers.character_api.httpx.get", return_value=response):
            with self.assertRaises(CharacterApiError) as denied:
                CharacterApiClient(
                    "expired", "OAuth test-client/1.0 (contact: test@example.com)"
                ).get_character("Alice")
        self.assertEqual(denied.exception.code, "character_api_auth_failed")
        self.assertNotIn("expired", denied.exception.reason)

    def test_角色列表必须符合官方包装结构(self) -> None:
        good = httpx.Response(
            200,
            json={"characters": [{"name": "A"}, None]},
            request=httpx.Request("GET", "https://api.pathofexile.com/character"),
        )
        with patch("parsers.character_api.httpx.get", return_value=good):
            self.assertEqual(CharacterApiClient(
                "token", "OAuth test-client/1.0 (contact: test@example.com)"
            ).list_characters(), [
                {"name": "A"},
            ])


class BuildRequestTest(unittest.TestCase):
    def test_code_或_character_name_必须且只能提供一个(self) -> None:
        self.assertEqual(BuildRequest(code="abc").code, "abc")
        self.assertEqual(BuildRequest(character_name="Alice").character_name, "Alice")
        for kwargs in ({}, {"code": "abc", "character_name": "Alice"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                BuildRequest(**kwargs)

    def test_character_name_进入统一_build_报告链路(self) -> None:
        class StubClient:
            def get_character(self, name: str) -> dict:
                self.name = name
                return _payload()

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(dbmod.SCHEMA)
        conn.executescript(dbmod.FTS_SCHEMA)
        try:
            with (
                patch("api.build._character_client", return_value=StubClient()),
                patch("api.build.dbmod.get_conn", return_value=conn),
            ):
                result = analyze_build(
                    BuildRequest(character_name="火花测试号"), caller="unit-test"
                )
        finally:
            conn.close()

        self.assertEqual(result.input_type, "character_api")
        self.assertEqual(result.character_name, "火花测试号")
        self.assertEqual(result.class_name, "Witch")
        self.assertEqual(result.alternate_ascendancy_name, "Nameless Bloodline")
        self.assertEqual(result.mastery_effects, {"200": "55"})
        self.assertEqual(len(result.report), 25)
        self.assertFalse(result.player_stats)


if __name__ == "__main__":
    unittest.main()
