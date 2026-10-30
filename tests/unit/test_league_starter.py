"""User-supplied PoB stage route and conservative numeric comparisons."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.deps import require_user
from api.planner import LeagueStarterRequest, league_starter
from main import create_app
from parsers.item_text import ParsedItem
from parsers.pob_build import (
    BuildGem, BuildItem, PobBuild, SkillGroup, encode_share_code,
)
from planner.league_starter import plan_league_starter


def build(
    *, life: float | None = 4000, dps: float | None = 100000,
    skill: str = "Arc", level: int = 70, tree: str = "3.29",
) -> PobBuild:
    stats = {}
    if life is not None:
        stats["Life"] = life
    if dps is not None:
        stats["CombinedDPS"] = dps
    return PobBuild(
        level=level, class_name="Witch", ascend_class_name="Elementalist",
        main_socket_group=1, calcs_skill_group=1,
        skill_groups=[SkillGroup(
            is_main=True, active_skill_index=1, active_skill_calcs_index=1,
            gems=(BuildGem(name_en=skill, skill_id=skill, level=10),),
        )],
        items=[BuildItem(slot="Helmet", raw="Old Helmet")],
        passive_nodes=("1", "2"),
        config={"enemyIsBoss": "Pinnacle"},
        player_stats=stats,
        tree_version=tree,
    )


def code(*, life: int = 4000, skill: str = "Arc", tree: str = "3_29") -> str:
    xml = (
        "<PathOfBuilding><Build level='70' className='Witch' "
        "ascendClassName='Elementalist' mainSocketGroup='1'>"
        f"<PlayerStat stat='Life' value='{life}'/>"
        "</Build><Config><Input name='enemyIsBoss' string='Pinnacle'/></Config>"
        "<Calcs><Input name='skill_number' number='1'/></Calcs>"
        f"<Skills><Skill mainActiveSkill='1'><Gem nameSpec='{skill}' "
        f"skillId='{skill}'/></Skill></Skills>"
        f"<Tree activeSpec='1'><Spec treeVersion='{tree}' nodes='1,2'/></Tree>"
        "</PathOfBuilding>"
    )
    return encode_share_code(xml)


class LeagueStarterDomainTest(unittest.TestCase):
    def test_adjacent_stages_are_ordered_and_structural_changes_are_not_causes(self) -> None:
        current = build()
        first = build(life=4500, dps=120000, level=80)
        first.items = [BuildItem(
            slot="Helmet", raw="New Helmet",
            parsed=ParsedItem(name="New Helmet", base_type="Hubris Circlet"),
        )]
        first.passive_nodes = ("1", "2", "3")
        second = build(life=5000, dps=150000, level=90)
        second.items = [BuildItem(slot="Helmet", raw="New Helmet")]
        second.passive_nodes = ("1", "2", "3", "4")

        plan = plan_league_starter(current, [
            ("白图", "完成白图目标", first),
            ("红图", "完成红图目标", second),
        ], metrics=["life", "dps"])
        self.assertEqual("ready", plan.status)
        self.assertEqual((4, 0), (plan.numeric_ready, plan.numeric_unknown))
        self.assertEqual("白图", plan.stages[1].previous_label)
        self.assertEqual(500, plan.stages[1].metrics[0].delta)
        self.assertEqual(("Helmet",), plan.stages[0].structure.changed_item_slots)
        self.assertEqual("New Helmet", plan.stages[0].equipment_targets[0].name)
        self.assertEqual("Hubris Circlet", plan.stages[0].equipment_targets[0].base_type)
        self.assertEqual(("4",), plan.stages[1].structure.passive_nodes_added)
        self.assertIn("不证明", plan.note)
        self.assertIn("重新计算", plan.stages[1].verification)

    def test_incomparable_skill_or_version_keeps_delta_unknown(self) -> None:
        plan = plan_league_starter(build(), [
            ("换技能", "用户选择换技能", build(skill="Fireball")),
            ("换版本", "用户选择新版本", build(skill="Fireball", tree="3.30")),
        ], metrics=["life"])
        self.assertEqual("unknown", plan.status)
        self.assertEqual(2, plan.numeric_unknown)
        self.assertTrue(all(stage.metrics[0].delta is None for stage in plan.stages))
        self.assertEqual("mismatch", plan.stages[0].context_status)
        self.assertEqual("mismatch", plan.stages[1].context_status)

    def test_missing_metric_is_unknown_while_other_metric_remains_ready(self) -> None:
        plan = plan_league_starter(build(), [
            ("阶段一", "用户目标", build(dps=None, life=4500)),
        ], metrics=["life", "dps"])
        self.assertEqual("partial", plan.status)
        self.assertEqual((1, 1), (plan.numeric_ready, plan.numeric_unknown))
        self.assertIsNone(plan.stages[0].metrics[1].delta)
        self.assertIn("缺少", plan.stages[0].metrics[1].reason)

    def test_bad_stage_contract_fails(self) -> None:
        current = build()
        for stages in ([], [("当前", "目标", current)], [("A", "", current)]):
            with self.subTest(stages=stages), self.assertRaises(ValueError):
                plan_league_starter(current, stages)


class LeagueStarterApiTest(unittest.TestCase):
    def test_schema_and_validation(self) -> None:
        valid = {
            "current_code": code(),
            "stages": [{"label": "白图", "goal": "推进地图", "code": code(life=4500)}],
            "metrics": ["life"],
        }
        schema = create_app().openapi()
        self.assertIn("/poe_agent/planner/league-starter", schema["paths"])
        for update in (
            {"stages": [{"label": "白图", "goal": "  ", "code": code()}]},
            {"stages": [valid["stages"][0], valid["stages"][0]]},
            {"metrics": ["life", "life"]},
        ):
            with self.subTest(update=update), self.assertRaises(ValidationError):
                LeagueStarterRequest.model_validate({**valid, **update})

    def test_endpoint_returns_stage_and_rejects_tree_only_input(self) -> None:
        request = LeagueStarterRequest.model_validate({
            "current_code": code(),
            "stages": [{"label": "白图", "goal": "推进地图", "code": code(life=4500)}],
            "metrics": ["life"],
        })
        result = league_starter(request)
        self.assertEqual("ready", result.status)
        self.assertEqual(500, result.stages[0].metrics[0].delta)
        self.assertEqual("推进地图", result.stages[0].goal)
        self.assertEqual("partial", result.stages[0].structure.status)

        request.current_code = "https://www.pathofexile.com/passive-skill-tree/3.29.0/abc"
        with self.assertRaises(HTTPException) as caught:
            league_starter(request)
        self.assertEqual(422, caught.exception.status_code)
        self.assertEqual("当前", caught.exception.detail["stage"])

    def test_route_requires_authentication(self) -> None:
        app = create_app()
        payload = {
            "current_code": code(),
            "stages": [{"label": "白图", "goal": "推进地图", "code": code(life=4500)}],
            "metrics": ["life"],
        }
        with patch("api.deps.user_keys", return_value=["planner-key"]):
            with TestClient(app) as client:
                self.assertEqual(401, client.post(
                    "/poe_agent/planner/league-starter", json=payload,
                ).status_code)
            app.dependency_overrides[require_user] = lambda: "test"
            with TestClient(app) as client:
                response = client.post("/poe_agent/planner/league-starter", json=payload)
        self.assertEqual(200, response.status_code)
        self.assertEqual(500, response.json()["stages"][0]["metrics"][0]["delta"])


if __name__ == "__main__":
    unittest.main()
