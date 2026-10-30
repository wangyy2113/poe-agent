"""Build 多方案比较：上下文口径、逐指标差值与安全边界。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from api.build import BuildCompareRequest, compare_builds
from main import create_app
from parsers.build_compare import compare_build_scenarios
from parsers.pob_build import BuildGem, BuildItem, PobBuild, SkillGroup, encode_share_code


def scenario(
    *,
    stats: dict[str, float],
    skill: str = "Arc",
    config: dict[str, str] | None = None,
    tree_version: str = "3.29",
) -> PobBuild:
    return PobBuild(
        level=95,
        class_name="Witch",
        ascend_class_name="Elementalist",
        skill_groups=[SkillGroup(
            is_main=True,
            active_skill_index=1,
            active_skill_calcs_index=1,
            gems=(BuildGem(name_en=skill, skill_id=skill),),
        )],
        main_socket_group=1,
        calcs_skill_group=1,
        config=dict(config or {"enemyIsBoss": "Pinnacle"}),
        player_stats=dict(stats),
        tree_version=tree_version,
    )


def pob_code(*, life: float, dps: float, skill: str = "Arc") -> str:
    xml = (
        "<PathOfBuilding><Build level='95' className='Witch' "
        "ascendClassName='Elementalist' mainSocketGroup='1'>"
        f"<PlayerStat stat='Life' value='{life}'/>"
        f"<PlayerStat stat='CombinedDPS' value='{dps}'/>"
        "</Build><Config><Input name='enemyIsBoss' string='Pinnacle'/></Config>"
        "<Calcs><Input name='skill_number' number='1'/></Calcs>"
        f"<Skills><Skill mainActiveSkill='1' mainActiveSkillCalcs='1'>"
        f"<Gem nameSpec='{skill}' skillId='{skill}'/></Skill></Skills>"
        "<Tree activeSpec='1'><Spec treeVersion='3_29' nodes=''/></Tree>"
        "</PathOfBuilding>"
    )
    return encode_share_code(xml)


class BuildCompareDomainTest(unittest.TestCase):
    def test_same_context_calculates_per_metric_absolute_and_percent_delta(self) -> None:
        result = compare_build_scenarios([
            ("当前", scenario(stats={"Life": 4000, "CombinedDPS": 100000})),
            ("方案 A", scenario(stats={"Life": 4500, "CombinedDPS": 120000})),
        ], metrics=["life", "dps"])

        self.assertEqual("ready", result.status)
        self.assertEqual("ready", result.context_status)
        self.assertEqual("当前", result.reference_label)
        life_candidate = result.metrics[0].values[1]
        self.assertEqual((life_candidate.delta, life_candidate.delta_percent), (500, 12.5))
        self.assertEqual("up", life_candidate.direction)
        self.assertEqual(20000, result.metrics[1].values[1].delta)
        self.assertEqual("not_ranked", result.overall_verdict)
        self.assertIn("不生成总体分数、总体最佳", result.note)

    def test_explicit_reference_label_changes_delta_direction(self) -> None:
        result = compare_build_scenarios([
            ("当前", scenario(stats={"Life": 4000})),
            ("方案 A", scenario(stats={"Life": 4500})),
        ], metrics=["life"], reference_label="方案 A")
        current = next(item for item in result.metrics[0].values if item.label == "当前")
        self.assertEqual(-500, current.delta)
        self.assertAlmostEqual(-11.111111, current.delta_percent or 0, places=5)
        self.assertEqual("down", current.direction)

    def test_skill_config_or_tree_mismatch_blocks_deltas(self) -> None:
        explicit_empty_config = scenario(stats={"Life": 4500})
        explicit_empty_config.config["conditionStationary"] = ""
        cases = (
            scenario(stats={"Life": 4500}, skill="Fireball"),
            scenario(stats={"Life": 4500}, config={"enemyIsBoss": "Uber"}),
            scenario(stats={"Life": 4500}, tree_version="3.30"),
            explicit_empty_config,
        )
        for candidate in cases:
            with self.subTest(candidate=candidate):
                result = compare_build_scenarios([
                    ("当前", scenario(stats={"Life": 4000})),
                    ("候选", candidate),
                ], metrics=["life"])
                self.assertEqual("unknown", result.status)
                self.assertEqual("mismatch", result.context_status)
                value = result.metrics[0].values[1]
                self.assertEqual("unknown", value.status)
                self.assertIsNone(value.delta)

    def test_missing_context_or_different_playerstat_field_is_unknown(self) -> None:
        no_skill = scenario(stats={"Life": 4500})
        no_skill.skill_groups = []
        no_skill.calcs_skill_group = None
        result = compare_build_scenarios([
            ("当前", scenario(stats={"Life": 4000})),
            ("无技能", no_skill),
        ], metrics=["life"])
        self.assertEqual("unknown", result.context_status)
        self.assertIn("没有可确认", result.metrics[0].values[1].reason)

        field_mismatch = compare_build_scenarios([
            ("当前", scenario(stats={"Life": 4000})),
            ("候选", scenario(stats={"LifeUnreserved": 4200, "Life": 5000})),
        ], metrics=["life"])
        self.assertIsNone(field_mismatch.metrics[0].values[1].delta)
        self.assertIn("字段口径不同", field_mismatch.metrics[0].values[1].reason)

    def test_missing_calcs_selection_or_config_cannot_claim_numeric_comparability(self) -> None:
        no_calcs = scenario(stats={"Life": 4500})
        no_calcs.calcs_skill_group = None
        missing_config = scenario(stats={"Life": 4500})
        missing_config.config = {}
        for candidate in (no_calcs, missing_config):
            with self.subTest(candidate=candidate):
                result = compare_build_scenarios([
                    ("当前", scenario(stats={"Life": 4000})),
                    ("候选", candidate),
                ], metrics=["life"])
                self.assertEqual("unknown", result.metrics[0].values[1].status)
                self.assertIsNone(result.metrics[0].values[1].delta)

    def test_partial_metric_coverage_keeps_known_comparison_only(self) -> None:
        result = compare_build_scenarios([
            ("当前", scenario(stats={"Life": 4000, "Armour": 10000})),
            ("完整", scenario(stats={"Life": 4500, "Armour": 12000})),
            ("缺护甲", scenario(stats={"Life": 4300})),
        ], metrics=["life", "armour"])
        self.assertEqual("partial", result.status)
        self.assertEqual((result.ready_metrics, result.partial_metrics), (1, 1))
        armour = result.metrics[1]
        self.assertEqual("partial", armour.status)
        self.assertEqual("ready", armour.values[1].status)
        self.assertEqual("unknown", armour.values[2].status)

    def test_structure_diff_lists_slots_gems_and_passives_without_causal_claim(self) -> None:
        reference = scenario(stats={"Life": 4000})
        reference.items = [BuildItem(slot="Helmet", raw="Old Helmet\n+10 Life")]
        reference.passive_nodes = ("1", "2", "3")
        reference.skill_groups = [SkillGroup(
            is_main=True,
            active_skill_index=1,
            active_skill_calcs_index=1,
            gems=(BuildGem(name_en="Arc", level=20, quality=20, skill_id="Arc"),),
        )]
        candidate = scenario(stats={"Life": 4500})
        candidate.items = [BuildItem(slot="Helmet", raw="Old Helmet\n+20 Life")]
        candidate.passive_nodes = ("2", "3", "4")
        candidate.skill_groups = [SkillGroup(
            is_main=True,
            active_skill_index=1,
            active_skill_calcs_index=1,
            gems=(
                BuildGem(name_en="Arc", level=21, quality=20, skill_id="Arc"),
                BuildGem(name_en="Added Lightning", skill_id="SupportAddedLightning"),
            ),
        )]

        result = compare_build_scenarios(
            [("当前", reference), ("候选", candidate)], metrics=["life"]
        )
        diff = result.structure_diffs[1]
        self.assertEqual(("Helmet",), diff.changed_item_slots)
        self.assertEqual(("Added Lightning", "Arc (L21, Q20)"), diff.added_gems)
        self.assertEqual(("Arc (L20, Q20)",), diff.removed_gems)
        self.assertEqual(("4",), diff.passive_nodes_added)
        self.assertEqual(("1",), diff.passive_nodes_removed)
        self.assertIn("不证明任何 PlayerStat 差值", diff.note)

    def test_zero_reference_keeps_absolute_delta_but_not_percentage(self) -> None:
        result = compare_build_scenarios([
            ("当前", scenario(stats={"ChaosResist": 0})),
            ("候选", scenario(stats={"ChaosResist": 20})),
        ], metrics=["chaos_resistance"])
        candidate = result.metrics[0].values[1]
        self.assertEqual(20, candidate.delta)
        self.assertIsNone(candidate.delta_percent)
        self.assertIn("基线值为 0", candidate.reason)

    def test_domain_rejects_bad_scenario_and_metric_contracts(self) -> None:
        build = scenario(stats={"Life": 4000})
        invalid_calls = (
            lambda: compare_build_scenarios([("only", build)]),
            lambda: compare_build_scenarios([("same", build), ("same", build)]),
            lambda: compare_build_scenarios(
                [("a", build), ("b", build)], metrics=[]
            ),
            lambda: compare_build_scenarios(
                [("a", build), ("b", build)], metrics=["life", "life"]
            ),
            lambda: compare_build_scenarios(
                [("a", build), ("b", build)], metrics=["not-real"]
            ),
            lambda: compare_build_scenarios(
                [("a", build), ("b", build)], reference_label="missing"
            ),
        )
        for call in invalid_calls:
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()


class BuildCompareApiTest(unittest.TestCase):
    def test_request_validation_and_openapi_contract(self) -> None:
        with self.assertRaises(ValidationError):
            BuildCompareRequest.model_validate({
                "scenarios": [
                    {"label": "same", "code": "a"},
                    {"label": "same", "code": "b"},
                ]
            })
        with self.assertRaises(ValidationError):
            BuildCompareRequest.model_validate({
                "scenarios": [
                    {"label": "a", "code": "a"},
                    {"label": "b", "code": "b"},
                ],
                "reference_label": "missing",
            })
        schema = create_app().openapi()
        operation = schema["paths"]["/poe_agent/build/compare"]["post"]
        self.assertEqual("BuildCompareResponse", operation["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"].rsplit("/", 1)[-1])

    def test_endpoint_decodes_two_pob_snapshots_and_returns_structured_deltas(self) -> None:
        request = BuildCompareRequest.model_validate({
            "scenarios": [
                {"label": "当前", "code": pob_code(life=4000, dps=100000)},
                {"label": "候选", "code": pob_code(life=4500, dps=125000)},
            ],
            "metrics": ["life", "dps"],
        })
        with (
            patch("api.build.dbmod.get_conn", return_value=object()),
            patch("api.build.bridge_gems", side_effect=lambda _conn, build: build),
        ):
            response = compare_builds(request, caller="test")
        self.assertEqual("ready", response.status)
        self.assertEqual(500, response.metrics[0].values[1].delta)
        self.assertEqual(25, response.metrics[1].values[1].delta_percent)
        self.assertEqual("not_ranked", response.overall_verdict)

    def test_endpoint_rejects_tree_only_input_with_scenario_identity(self) -> None:
        request = BuildCompareRequest.model_validate({
            "scenarios": [
                {
                    "label": "树链接",
                    "code": "https://www.pathofexile.com/passive-skill-tree/3.29.0/abc",
                },
                {"label": "候选", "code": pob_code(life=4500, dps=125000)},
            ]
        })
        with self.assertRaises(HTTPException) as caught:
            compare_builds(request, caller="test")
        self.assertEqual(422, caught.exception.status_code)
        self.assertEqual("树链接", caught.exception.detail["scenario"])
        self.assertIn("不含 PlayerStat", caught.exception.detail["reason"])


if __name__ == "__main__":
    unittest.main()
