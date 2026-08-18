"""Body Armour normal affix pool：底材桥接、条件权重与编排边界。"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.db import SCHEMA  # noqa: E402
from core.llm import LLMResponse, StreamEvent, ToolCall  # noqa: E402
from craft.pool import (  # noqa: E402
    POOL_SNAPSHOT_MODE,
    affix_page_digest,
    analyze_affix_pool,
)
from kb.pipeline import _replace_mod_page  # noqa: E402
from kb.sources import poedb_mods  # noqa: E402
from kb.sources.pob import PoBRepo  # noqa: E402
from kb.sources.pob_bases import (  # noqa: E402
    BaseStructureError,
    extract_body_armour_bases,
)
from orchestrator import binding, loop  # noqa: E402
from orchestrator.binding import number_evidence  # noqa: E402
from scripts.import_pob_bases import (  # noqa: E402
    replace_snapshot,
    validate_snapshot_size,
)
from tools.impl.craft_pool import affix_pool_odds  # noqa: E402
from tools.registry import ToolResult  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
POB_FIXTURE = FIXTURES / "pob_body_sample"
CN_FIXTURE = FIXTURES / "poedb_body_armours_cn_sample.html"
EN_FIXTURE = FIXTURES / "poedb_body_armours_us_sample.html"
COMMIT = "9e6ea223e5921abd00bab36f246072e353140a58"
QUESTION = (
    "请查询 normal 普通词缀有效池：底材“铁制背心”，物品等级 50，"
    "前缀侧，目标“最大生命”的原始权重和条件概率。"
)
TOOL_ARGS = {
    "base_type": "铁制背心",
    "item_level": 50,
    "generation_side": "prefix",
    "target_query": "最大生命",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def _repo() -> PoBRepo:
    return PoBRepo(
        root=POB_FIXTURE,
        origin="test fixture",
        repo_url="https://github.com/PathOfBuildingCommunity/PathOfBuilding",
        commit=COMMIT,
        manifest_version="2.66.2",
        tree_version="3_29",
    )


def _bases():  # noqa: ANN202
    return extract_body_armour_bases(
        POB_FIXTURE, _read(CN_FIXTURE), _read(EN_FIXTURE)
    )


def _insert_mod(
    conn: sqlite3.Connection,
    *,
    uid: str,
    group: str,
    weight: int,
    text: str,
    ilvl: int = 1,
    gen_type: int = 1,
    family: tuple[str, ...] | None = None,
) -> None:
    families = family or (group,)
    conn.execute(
        """
        INSERT INTO mod_affix(
            uid,mod_code,item_class_page,item_class_code,item_class_zh,category,
            category_label,name_zh,gen_type,gen_label,family_list,correct_group,
            ilvl,tier,tier_count,tier_source,weight,weight_pct,text_zh,
            text_template,loose_template,ranges,tags,spawn_tags,fossil_tags,
            variant_id,source,source_priority,url,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        """,
        (
            uid, uid, "Body_Armours_str", "Body Armour", "胸甲", "normal",
            "普通", group, gen_type, "前缀" if gen_type == 1 else "后缀",
            json.dumps(families, ensure_ascii=False), group, ilvl, 1, 1,
            "poedb_payload", weight, 100.0, text, text, text, "[]", "[]",
            "[]", "[]", "", "POEDB", 1,
            f"https://poedb.tw/cn/Modifiers/{uid}",
        ),
    )


def _write_page_audit(
    conn: sqlite3.Connection, *, fetched_at: str | None = None,
    source_total_rows: int | None = None, source_failed_rows: int = 0,
    source_final_url: str = "https://poedb.tw/cn/Body_Armours_str",
) -> None:
    count, digest = affix_page_digest(conn, "Body_Armours_str")
    total = count if source_total_rows is None else source_total_rows
    conn.execute(
        "INSERT INTO ingest_run(source,category,started_at,finished_at,total,succeeded,"
        "failed,note) VALUES ('POEDB','mod_affix',datetime('now'),datetime('now'),1,1,0,?)",
        (
            json.dumps(
                {
                    "snapshot_mode": POOL_SNAPSHOT_MODE,
                    "pages": {
                        "Body_Armours_str": {
                            "status": "complete",
                            "rows": count,
                            "digest": digest,
                            "source_url": "https://poedb.tw/cn/Body_Armours_str",
                            "source_final_url": source_final_url,
                            "source_content_hash": "a" * 64,
                            "source_fetched_at": fetched_at or datetime.now(
                                timezone.utc
                            ).isoformat(timespec="seconds"),
                            "source_total_rows": total,
                            "source_succeeded_rows": count,
                            "source_failed_rows": source_failed_rows,
                            "source_deduped_rows": max(total - count, 0),
                            "source_dedup_conflicts": 0,
                            "parse_complete": True,
                            "explicit_no_mods": False,
                        }
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        ),
    )
    conn.commit()


def _populated_conn(*, audit: bool = True) -> sqlite3.Connection:
    conn = _conn()
    replace_snapshot(conn, _bases(), _repo(), 2)
    _insert_mod(
        conn, uid="life-low", group="IncreasedLife", weight=100,
        text="+(10-19) 最大生命", ilvl=1,
    )
    _insert_mod(
        conn, uid="life-high", group="IncreasedLife", weight=50,
        text="+(60-69) 最大生命", ilvl=40,
    )
    _insert_mod(
        conn, uid="armour", group="LocalIncreasedArmour", weight=300,
        text="增加 (50-59)% 护甲", ilvl=1,
    )
    _insert_mod(
        conn, uid="mana", group="IncreasedMana", weight=50,
        text="+(20-24) 最大魔力", ilvl=1,
    )
    _insert_mod(
        conn, uid="suffix-fire", group="FireResistance", weight=200,
        text="+(10-19)% 火焰抗性", ilvl=1, gen_type=2,
    )
    if audit:
        _write_page_audit(conn)
    else:
        conn.commit()
    return conn


class TestBodyArmourBaseExtractor(unittest.TestCase):
    def test_真实裁剪样本逐字桥接且稳定键哈希可复现(self):
        first = _bases()
        second = _bases()
        self.assertEqual(len(first), 3)
        self.assertEqual(
            [(base.name_en, base.name_zh, base.modifier_page) for base in first],
            [
                ("Chestplate", "胸甲", "Body_Armours_str"),
                ("Copper Plate", "铜锻板甲", "Body_Armours_str"),
                ("Plate Vest", "铁制背心", "Body_Armours_str"),
            ],
        )
        self.assertEqual(
            [(base.base_id, base.content_hash) for base in first],
            [(base.base_id, base.content_hash) for base in second],
        )
        self.assertTrue(all(base.armour_tag == "str_armour" for base in first))

    def test_关键字段Lua表达式与跨语言slug偏差均整批拒绝(self):
        lua_path = POB_FIXTURE / "src" / "Data" / "Bases" / "body.lua"
        original = _read(lua_path)
        import tempfile

        cases = (
            (original.replace("socketLimit = 6,", "socketLimit = 0,", 1), _read(CN_FIXTURE), _read(EN_FIXTURE)),
            (original.replace("ArmourBaseMin = 19", "ArmourBaseMin = unknown()", 1), _read(CN_FIXTURE), _read(EN_FIXTURE)),
            (original, _read(CN_FIXTURE).replace("/cn/Plate_Vest", "/cn/Plate_Vest_Changed", 1), _read(EN_FIXTURE)),
        )
        for lua, cn_html, en_html in cases:
            with self.subTest():
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    target = root / "src" / "Data" / "Bases" / "body.lua"
                    target.parent.mkdir(parents=True)
                    target.write_text(lua, encoding="utf-8")
                    with self.assertRaises(BaseStructureError):
                        extract_body_armour_bases(root, cn_html, en_html)

    def test_英文名多slug命中拒绝而不是猜一个(self):
        cn = _read(CN_FIXTURE).replace(
            "</body>", '<a class="whiteitem BodyArmour" href="/cn/Plate_Vest_2">铁制背心二</a></body>'
        )
        en = _read(EN_FIXTURE).replace(
            "</body>", '<a class="whiteitem BodyArmour" href="/us/Plate_Vest_2">Plate Vest</a></body>'
        )
        with self.assertRaisesRegex(BaseStructureError, "命中 2 个 slug"):
            extract_body_armour_bases(POB_FIXTURE, cn, en)


class TestBaseSnapshotStorage(unittest.TestCase):
    def test_独立表保留P1P2字段且替换删除旧记录(self):
        conn = _conn()
        try:
            replace_snapshot(conn, _bases(), _repo(), 2)
            row = conn.execute(
                "SELECT name_en,name_zh,source,source_priority,source_commit,pob_url,"
                "poedb_url FROM pob_base_item WHERE name_en='Plate Vest'"
            ).fetchone()
            self.assertEqual(tuple(row[:5]), ("Plate Vest", "铁制背心", "Path of Building", 2, COMMIT))
            self.assertIn(f"/blob/{COMMIT}/", row[5])
            self.assertEqual(row[6], "https://poedb.tw/cn/Plate_Vest")
            replace_snapshot(conn, _bases()[:1], _repo(), 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM pob_base_item").fetchone()[0], 1)
        finally:
            conn.close()

    def test_首次过小和已有快照骤降默认拒绝(self):
        conn = _conn()
        try:
            with self.assertRaisesRegex(BaseStructureError, "绝对下限"):
                validate_snapshot_size(conn, 3)

            class ExistingSnapshot:
                @staticmethod
                def execute(_sql):  # noqa: ANN001
                    class Cursor:
                        @staticmethod
                        def fetchone():
                            return (126,)
                    return Cursor()

            # 相对缩减护栏只在**过了绝对下限之后**才有机会触发，所以两条护栏的
            # 可触发区间是相互约束的：既有行数必须大于 下限/(1-20%) ≈ 1125，
            # 否则「缩掉 20%」的结果早已低于绝对下限 900，先撞的是下限那条。
            # 当前实际是 1022 行，所以线上是绝对下限在守；这里用 1500 才测得到相对那条。
            class BigSnapshot:
                @staticmethod
                def execute(_sql):  # noqa: ANN001
                    class Cursor:
                        @staticmethod
                        def fetchone():
                            return (1500,)
                    return Cursor()

            with self.assertRaisesRegex(BaseStructureError, "相对缩减"):
                validate_snapshot_size(BigSnapshot(), 1000)
            validate_snapshot_size(BigSnapshot(), 1000, allow_shrink=True)
            # 单类别导入时下限由调用方给（胸甲仍是 100）
            with self.assertRaisesRegex(BaseStructureError, "绝对下限 100"):
                validate_snapshot_size(ExistingSnapshot(), 50, minimum=100)
        finally:
            conn.close()


class TestModPageSnapshotTransaction(unittest.TestCase):
    def test_整页替换删除旧行且返回可复核摘要(self):
        conn = _populated_conn(audit=False)
        try:
            count, digest = _replace_mod_page(conn, "Body_Armours_str", [])
            self.assertEqual(count, 0)
            self.assertEqual((count, digest), affix_page_digest(conn, "Body_Armours_str"))
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM mod_affix WHERE item_class_page='Body_Armours_str'"
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_替换中途失败回滚保留旧页(self):
        conn = _populated_conn(audit=False)
        try:
            before = affix_page_digest(conn, "Body_Armours_str")
            with patch("kb.pipeline._upsert_mods", side_effect=RuntimeError("boom")):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    _replace_mod_page(conn, "Body_Armours_str", [object()])
            self.assertEqual(affix_page_digest(conn, "Body_Armours_str"), before)
        finally:
            conn.close()

    def test_只有身份可核对的显式空页才允许NoMods(self):
        slug = "Assembled_Eye_Jewel"
        url = f"https://poedb.tw/cn/{slug}"
        verified_empty = (
            '<!DOCTYPE html><html lang="cn"><head><title>聚合之凝珠宝</title></head><body>'
            f'<a href="/cn/{slug}">CN</a>'
            f'<a href="/cn/marked?id={slug}">Edit</a>'
            '<table><tr><td>BaseType</td><td>Assembled Eye Jewel</td></tr></table>'
            '<footer>Copyright</footer>'
            '<script src="https://cdn.poedb.tw/js/poedb_footer.test.js"></script>'
            '</body></html>'
        )
        with self.assertRaises(poedb_mods.NoModsDataError):
            poedb_mods.extract_mods(verified_empty, slug, url)
        truncated = verified_empty.split("<footer>", 1)[0]
        with self.assertRaisesRegex(poedb_mods.ModStructureError, "拒绝用它清空旧页"):
            poedb_mods.extract_mods(truncated, slug, url)
        with self.assertRaisesRegex(poedb_mods.ModStructureError, "拒绝用它清空旧页"):
            poedb_mods.extract_mods("<html><title>challenge</title></html>", slug, url)

    def test_normal行缺权重时整页解析失败而非静默当零(self):
        payload = {
            "baseitem": {
                "href": "Body_Armours", "cn": "Body Armour", "tags": "Str",
                "link_name": "胸甲",
            },
            "opt": {"ItemClassesCode": "Body Armour"},
            "config": {"normal": {"title": "普通"}},
            "gen": {"1": "前缀"},
            "normal": [{
                "ModGenerationTypeID": "1", "ModFamilyList": ["Life"],
                "Level": 1, "Code": "MissingWeight", "str": "+# 最大生命",
            }],
        }

        def page_html(data: dict[str, object]) -> str:
            return (
                '<!DOCTYPE html><html lang="cn"><head><title>'
                'Body_Armours_str - POEDB</title></head><body>'
                '<a href="/cn/Body_Armours_str">CN</a>'
                '<a href="/cn/marked?id=Body_Armours_str">Edit</a>'
                '<div id="ModifiersCalc"></div>'
                f'<script>new ModsView({json.dumps(data)})</script>'
                '<footer>Copyright</footer>'
                '<script src="https://cdn.poedb.tw/js/poedb_footer.test.js"></script>'
                '</body></html>'
            )

        with self.assertRaisesRegex(poedb_mods.ModStructureError, "完整池要求零丢失"):
            poedb_mods.extract_mods(
                page_html(payload),
                "Body_Armours_str",
                "https://poedb.tw/cn/Body_Armours_str",
            )

        wrong_identity = json.loads(json.dumps(payload))
        wrong_identity["baseitem"]["tags"] = "Dex"
        with self.assertRaisesRegex(poedb_mods.ModStructureError, "载荷身份不匹配"):
            poedb_mods.extract_mods(
                page_html(wrong_identity),
                "Body_Armours_str",
                "https://poedb.tw/cn/Body_Armours_str",
            )


class TestAffixPoolMathAndAudit(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _populated_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_完整池手算使用整数权重且不使用组内百分比(self):
        report = analyze_affix_pool(self.conn, **TOOL_ARGS)
        self.assertEqual(report.status, "exact")
        self.assertEqual(report.pool_size, 4)
        self.assertEqual(report.total_weight, 500)
        self.assertEqual(report.target_weight, 150)
        self.assertEqual(
            (report.probability_numerator, report.probability_denominator), (3, 10)
        )
        self.assertEqual(report.probability_decimal, "0.3")
        self.assertEqual(report.expected_independent_selections, "10/3")
        self.assertEqual({row.uid for row in report.target_rows}, {"life-low", "life-high"})

    def test_digest稳定且权重变化后旧审计拒绝新审计产生新digest(self):
        first = analyze_affix_pool(self.conn, **TOOL_ARGS)
        second = analyze_affix_pool(self.conn, **TOOL_ARGS)
        self.assertEqual(first.pool_digest, second.pool_digest)
        self.conn.execute("UPDATE mod_affix SET weight=51 WHERE uid='life-high'")
        stale = analyze_affix_pool(self.conn, **TOOL_ARGS)
        self.assertEqual(stale.status, "unknown")
        self.assertEqual(stale.gaps[0].code, "pool_snapshot_unverified")
        _write_page_audit(self.conn)
        changed = analyze_affix_pool(self.conn, **TOOL_ARGS)
        self.assertEqual(changed.status, "exact")
        self.assertNotEqual(first.pool_digest, changed.pool_digest)

    def test_没有整页替换审计绝不声称完整池(self):
        conn = _populated_conn(audit=False)
        try:
            report = analyze_affix_pool(conn, **TOOL_ARGS)
            self.assertEqual(report.status, "unknown")
            self.assertEqual(report.gaps[0].code, "pool_snapshot_unverified")
            self.assertIsNone(report.probability_numerator)
        finally:
            conn.close()

    def test_部分解析自摘要与过期来源均不能自证exact(self):
        self.conn.execute("DELETE FROM ingest_run WHERE category='mod_affix'")
        _write_page_audit(
            self.conn,
            source_final_url="https://poedb.tw/cn/Body_Armours_dex",
        )
        redirected = analyze_affix_pool(self.conn, **TOOL_ARGS)
        self.assertEqual(redirected.status, "unknown")
        self.assertIn("审计字段无效", redirected.gaps[0].message)

        self.conn.execute("DELETE FROM ingest_run WHERE category='mod_affix'")
        _write_page_audit(self.conn, source_failed_rows=1)
        partial = analyze_affix_pool(self.conn, **TOOL_ARGS)
        self.assertEqual(partial.status, "unknown")
        self.assertIn("零丢失", partial.gaps[0].message)

        self.conn.execute("DELETE FROM ingest_run WHERE category='mod_affix'")
        stale_time = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(
            timespec="seconds"
        )
        _write_page_audit(self.conn, fetched_at=stale_time)
        stale = analyze_affix_pool(self.conn, **TOOL_ARGS)
        self.assertEqual(stale.status, "unknown")
        self.assertIn("过期", stale.gaps[0].message)

    def test_多组与零命中都是unknown而不是零概率(self):
        ambiguous = analyze_affix_pool(
            self.conn, **{**TOOL_ARGS, "target_query": "最大"}
        )
        missing = analyze_affix_pool(
            self.conn, **{**TOOL_ARGS, "target_query": "不存在的词缀"}
        )
        self.assertEqual(ambiguous.status, "unknown")
        self.assertEqual(ambiguous.gaps[0].code, "target_ambiguous")
        self.assertEqual(
            {item["correct_group"] for item in ambiguous.candidate_groups},
            {"IncreasedLife", "IncreasedMana"},
        )
        self.assertEqual(missing.status, "unknown")
        self.assertEqual(missing.gaps[0].code, "target_not_resolved")
        self.assertIsNone(missing.probability_decimal)

    def test_未知底材与损坏family均unknown(self):
        unknown = analyze_affix_pool(
            self.conn, **{**TOOL_ARGS, "base_type": "泛称胸甲"}
        )
        self.assertEqual(unknown.gaps[0].code, "base_not_resolved")
        self.conn.execute("UPDATE mod_affix SET family_list='[]' WHERE uid='armour'")
        _write_page_audit(self.conn)
        incomplete = analyze_affix_pool(self.conn, **TOOL_ARGS)
        self.assertEqual(incomplete.gaps[0].code, "pool_incomplete")


class TestAffixPoolToolBindingAndRoute(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _populated_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def _tool_result(self) -> ToolResult:
        with patch("tools.impl.craft_pool.dbmod.get_conn", return_value=self.conn):
            return affix_pool_odds(**TOOL_ARGS)

    def test_tool_exact必须返回P2底材_跨源映射与P1池三类Citation(self):
        result = self._tool_result()
        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], "exact")
        self.assertEqual(result.data["probability_decimal"], "0.3")
        self.assertEqual(len(result.citations), 3)
        self.assertEqual(
            [(c["source_priority"], c["channel"], c["claim_data"]["kind"]) for c in result.citations],
            [
                (2, "structured", "pob_base"),
                (1, "derived", "base_mapping"),
                (1, "derived", "pool_odds"),
            ],
        )
        self.assertEqual(
            [c["chunk_id"].split(":", 1)[0] for c in result.citations],
            ["affix_pool_pob_base", "affix_pool_base_mapping", "affix_pool"],
        )
        self.assertEqual(loop._precision_citations("affix_pool_odds", result), result.citations)
        one_missing = ToolResult(ok=True, data=result.data, citations=result.citations[:2])
        self.assertEqual(loop._precision_citations("affix_pool_odds", one_missing), [])

        mismatched = [dict(c) for c in result.citations]
        mismatched[1] = {
            **mismatched[1],
            "claim_data": {**mismatched[1]["claim_data"], "modifier_page": "Body_Armours_dex"},
        }
        self.assertEqual(
            loop._precision_citations(
                "affix_pool_odds", ToolResult(ok=True, data=result.data, citations=mismatched)
            ),
            [],
        )

    def test_binding批量流式只放行固定窄句式并拦截外推(self):
        evidence = number_evidence(self._tool_result().citations)
        allowed = (
            "底材“铁制背心”（Plate Vest）映射到 POEDB 词缀页“Body_Armours_str”[[E1]][[E2]]。"
            "在 normal 来源、物品等级 50、前缀侧且未应用已有词缀 family 排除的条件下，"
            "有效池总权重为 500，目标词缀组“IncreasedLife”权重为 150，条件概率为 3/10（0.3）[[E1]][[E2]][[E3]]。"
            "在相同条件的独立同分布选择模型中，命中目标词缀组“IncreasedLife”的数学期望选择次数为 10/3[[E1]][[E2]][[E3]]。"
            "该条件概率不是任何通货或具体工艺的一次使用成功率[[E3]]。"
        )
        batch = binding.verify(allowed, evidence)
        gate = binding.StreamingGate(evidence)
        gate.feed(allowed[:80])
        gate.feed(allowed[80:])
        gate.flush()
        streamed = gate.result()
        self.assertEqual((batch.blocked, batch.text), (0, allowed))
        self.assertEqual((streamed.blocked, streamed.text), (0, allowed))

        forbidden = (
            "使用混沌石的一次成功率为 30%[[E3]]。",
            "平均需要 10/3 个混沌石[[E3]]。",
            "第一步先锁前缀再操作[[E3]]。",
            "这是最快且最经济的制作路线[[E3]]。",
            "当前装备还剩一个前缀位置[[E3]]。",
            "建议准备 10/3 个混沌石。",
            "推荐把 30% 当作混沌石成功率。",
            "建议第一步先用混沌石，再锁前缀。",
        )
        for sentence in forbidden:
            with self.subTest(sentence=sentence):
                self.assertEqual(binding.verify(sentence, evidence).blocked, 1)
                stream_gate = binding.StreamingGate(evidence)
                self.assertEqual(stream_gate.feed(sentence), [])
                stream_gate.flush()
                self.assertEqual(stream_gate.result().blocked, 1)

        only_pool = (
            "在 normal 来源、物品等级 50、前缀侧且未应用已有词缀 family 排除的条件下，"
            "有效池总权重为 500，目标词缀组“IncreasedLife”权重为 150，条件概率为 3/10（0.3）[[E3]]。"
        )
        self.assertEqual(binding.verify(only_pool, evidence).blocked, 1)
        stream_gate = binding.StreamingGate(evidence)
        self.assertEqual(stream_gate.feed(only_pool), [])
        self.assertTrue(all(not verdict.ok for verdict in stream_gate.flush()))

    def test_精确问法冻结参数且特殊来源缺字段与泛称不路由(self):
        self.assertEqual(loop._precision_tool_route(QUESTION), "affix_pool_odds")
        self.assertEqual(
            loop._validate_precision_call(QUESTION, "affix_pool_odds", TOOL_ARGS), ""
        )
        invalid_args = (
            {**TOOL_ARGS, "item_level": 51},
            {**TOOL_ARGS, "base_type": "Plate Vest"},
            {**TOOL_ARGS, "generation_side": "suffix"},
            {**TOOL_ARGS, "target_query": "生命"},
            {**TOOL_ARGS, "extra": True},
        )
        for args in invalid_args:
            self.assertTrue(loop._validate_precision_call(QUESTION, "affix_pool_odds", args))
        rejected = (
            "普通有效池：底材“铁制背心”，前缀侧，目标“最大生命”的条件概率。",
            "normal 有效池：底材“铁制背心”，物品等级 50，前缀侧，目标“最大生命”的混沌石成功率。",
            "normal 有效池：底材“铁制背心”，物品等级 50，前缀侧，目标“最大生命”的化石条件概率。",
            "normal 有效池：底材“铁制背心”，物品等级 50，前缀和后缀，目标“最大生命”的条件概率。",
            QUESTION + " 并给我造价。",
            QUESTION + " 再给出操作流程。",
            QUESTION + " 推荐最划算做法。",
            QUESTION + " 平均要多少通货。",
        )
        for question in rejected:
            self.assertNotEqual(loop._precision_tool_route(question), "affix_pool_odds")

    def test_schema裁剪与执行层二次授权均拒绝伪造调用(self):
        schemas = [
            {"type": "function", "function": {"name": "kb_search"}},
            {"type": "function", "function": {"name": "affix_pool_odds"}},
        ]
        with patch("orchestrator.loop._prepare_tools", return_value=schemas):
            self.assertEqual(
                [s["function"]["name"] for s in loop._tools_for_question("最大生命是什么？")],
                ["kb_search"],
            )
            self.assertEqual(
                [s["function"]["name"] for s in loop._tools_for_question(QUESTION)],
                ["kb_search", "affix_pool_odds"],
            )
        with patch("orchestrator.loop.REGISTRY.call") as registry_call:
            result, matched = loop._call_tool(
                "最大生命是什么？", "", "affix_pool_odds",
                json.dumps(TOOL_ARGS, ensure_ascii=False),
            )
        self.assertFalse(result.ok)
        self.assertFalse(matched)
        registry_call.assert_not_called()


class TestAffixPoolOrchestratorEndToEnd(unittest.TestCase):
    class _Config:
        values = {
            "llm.max_tokens_per_turn": 1000,
            "llm.max_tool_calls": 2,
            "retrieval.factual_terms": [],
        }

        def get(self, key, default=None):
            return self.values.get(key, default)

    class _ChatClient:
        def __init__(self, responses):
            self.responses = iter(responses)

        def chat(self, _messages, **_kwargs):
            return next(self.responses)

    class _StreamClient:
        def __init__(self, rounds):
            self.rounds = iter(rounds)

        def stream(self, _messages, **_kwargs):
            yield from next(self.rounds)

    @staticmethod
    def _kb_result() -> ToolResult:
        return ToolResult(
            ok=True,
            data={"sufficient": False, "reason": "BM25 依据不足", "missing_terms": ["条件池"]},
            meta={"channels": ["bm25"], "degraded": False},
        )

    def _patches(self, conn: sqlite3.Connection):
        def registry_call(name, raw):
            if name == "kb_search":
                return self._kb_result()
            if name == "affix_pool_odds":
                return affix_pool_odds(**json.loads(raw))
            raise AssertionError(name)

        stack = ExitStack()
        stack.enter_context(patch("orchestrator.loop.get_config", return_value=self._Config()))
        stack.enter_context(patch("orchestrator.loop.build_system_prompt", return_value="system"))
        stack.enter_context(patch("orchestrator.loop._prepare_tools", return_value=[]))
        stack.enter_context(patch("orchestrator.loop._domain_terms", return_value=()))
        stack.enter_context(patch("orchestrator.loop._persist"))
        stack.enter_context(patch("orchestrator.loop._record_gaps"))
        stack.enter_context(patch("orchestrator.loop.dbmod.get_conn", return_value=conn))
        stack.enter_context(patch("tools.impl.craft_pool.dbmod.get_conn", return_value=conn))
        stack.enter_context(patch("orchestrator.loop.REGISTRY.call", side_effect=registry_call))
        return stack

    @staticmethod
    def _answer() -> str:
        return (
            "底材“铁制背心”（Plate Vest）映射到 POEDB 词缀页“Body_Armours_str”[[E1]][[E2]]。"
            "在 normal 来源、物品等级 50、前缀侧且未应用已有词缀 family 排除的条件下，"
            "有效池总权重为 500，目标词缀组“IncreasedLife”权重为 150，条件概率为 3/10（0.3）[[E1]][[E2]][[E3]]。"
            "在相同条件的独立同分布选择模型中，命中目标词缀组“IncreasedLife”的数学期望选择次数为 10/3[[E1]][[E2]][[E3]]。"
            "该条件概率不是任何通货或具体工艺的一次使用成功率[[E3]]。"
        )

    def test_batch与stream在KB不足时仍强制精确工具后再放行(self):
        conn = _populated_conn()
        call = ToolCall(
            id="affix-1", name="affix_pool_odds",
            arguments=json.dumps(TOOL_ARGS, ensure_ascii=False),
        )
        try:
            with self._patches(conn):
                batch = loop.run_turn(
                    QUESTION,
                    client=self._ChatClient([
                        LLMResponse(content="", tool_calls=(call,)),
                        LLMResponse(content=self._answer()),
                    ]),
                )
            self.assertFalse(batch.evidence_gap)
            self.assertEqual(batch.retrieval_meta["precision_route"], "affix_pool_odds")
            self.assertEqual(
                [e.claim_data["kind"] for e in batch.evidence],
                ["pob_base", "base_mapping", "pool_odds"],
            )
            self.assertEqual(batch.blocked, 0)
            self.assertEqual([citation.label for citation in batch.citations], ["E1", "E2", "E3"])

            with self._patches(conn):
                events = list(loop.stream_turn(
                    QUESTION,
                    client=self._StreamClient([
                        [StreamEvent(kind="tool_calls", tool_calls=(call,))],
                        [StreamEvent(kind="content", text=self._answer())],
                    ]),
                ))
            self.assertFalse(any(event.get("type") == "gap" for event in events))
            self.assertTrue(any(event.get("type") == "citations" for event in events))
            self.assertEqual(
                "".join(event.get("text", "") for event in events if event.get("type") == "sentence"),
                self._answer(),
            )
        finally:
            conn.close()

    def test_unknown在batch与stream均覆盖模型伪造概率并记录缺口(self):
        conn = _populated_conn()
        ambiguous_args = {**TOOL_ARGS, "target_query": "最大"}
        question = QUESTION.replace("目标“最大生命”", "目标“最大”")
        call = ToolCall(
            id="affix-unknown", name="affix_pool_odds",
            arguments=json.dumps(ambiguous_args, ensure_ascii=False),
        )
        fake = "混沌石成功率为 50%[[E1]]。"
        try:
            with self._patches(conn), patch("orchestrator.loop._record_final_tool_result_gaps") as record:
                batch = loop.run_turn(
                    question,
                    client=self._ChatClient([
                        LLMResponse(content="", tool_calls=(call,)),
                        LLMResponse(content=fake),
                    ]),
                )
            self.assertTrue(batch.evidence_gap)
            self.assertIn("必须先消歧", batch.answer)
            self.assertIn("不能解释为 0%", batch.answer)
            self.assertNotIn("50%", batch.answer)
            self.assertEqual(batch.evidence, [])
            record.assert_called_once()

            with self._patches(conn), patch("orchestrator.loop._record_final_tool_result_gaps") as record:
                events = list(loop.stream_turn(
                    question,
                    client=self._StreamClient([
                        [StreamEvent(kind="tool_calls", tool_calls=(call,))],
                        [StreamEvent(kind="content", text=fake)],
                    ]),
                ))
            gaps = [event for event in events if event.get("type") == "gap"]
            self.assertEqual(len(gaps), 1)
            self.assertIn("必须先消歧", gaps[0]["text"])
            self.assertFalse(any("50%" in event.get("text", "") for event in events))
            self.assertFalse(any(event.get("type") == "sentence" for event in events))
            record.assert_called_once()
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
