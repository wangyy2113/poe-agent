"""技能石的获取途径（POEDB 详情页的「使命」表，第四十三轮）。

全部离线：HTML 在内存里构造，不读 `data/http_cache`、不连网。

这一维**只有 POEDB 一个来源**（PoB 的 `Gems.lua` 没有任务奖励数据），
所以没有跨来源对账可做，用例分三组：

1. 解析：三种获取途径都要认出来，尤其是**起始箱那 21 行 NPC 单元格是空的**；
2. 归并：同一个主键下两行要**取职业并集**——直接丢一行会少掉一个职业；
3. 出口：卡片必须真的写出来，而那 152 颗没有使命表的要写**中性事实**
   而不是推断「所以靠掉落」。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kb import render_skills  # noqa: E402
from kb.sources.poedb_gem_page import (  # noqa: E402
    CLASSES,
    GemPageError,
    GemQuestRow,
    parse_gem_quests,
)
from scripts.import_gem_quests import merge_rows  # noqa: E402

# ── 构造详情页 ─────────────────────────────────────────────────

_HEAD = "<tr><th>章节</th><th>使命</th><th>NPC</th><th>角色</th></tr>"


def npc_cell(kind: str, npc: str = "") -> str:
    """NPC 列。三种形态都照真实页面写。"""
    if kind == "box":
        # **起始箱那一列是空的**，只有图标带着 title。
        return '<i title="Box"></i>'
    if kind == "vendor":
        return f'<i title="任务商人"></i><a href="/cn/Nessa">{npc}</a>'
    if kind == "no_icon":
        # 实测 4 行上游漏了图标，但文本仍是「任务奖励」。
        return "任务奖励"
    return '<i title="任务奖励"></i>任务奖励'


def quest_page(rows: list[tuple[str, str, str, str]], *, before: str = "") -> str:
    """真实页面上使命表前面还有别的表（逐级表、品类），所以默认塞一张进去——
    按下标取表会取错，这一点与 `test_gem_stats` 的构造保持一致。"""
    body = "".join(
        f"<tr><td>{act}</td>"
        f'<td><a class="questitem" href="/cn/{slug}">{quest}</a></td>'
        f"<td>{npc}</td><td>{roles}</td></tr>"
        for act, quest, slug, npc, roles in
        [(a, q, q.replace(" ", "_"), n, r) for a, q, n, r in rows]
    )
    other = before or "<table><thead><tr><th>等级</th><th>需求等级</th></tr></thead></table>"
    return (
        f"{other}<table><thead>{_HEAD}</thead><tbody>{body}</tbody></table>"
    )


class ParseTest(unittest.TestCase):
    def test_任务奖励与商人两种途径(self) -> None:
        rows = parse_gem_quests(
            quest_page([
                ("1", "海妖之歌", npc_cell("quest_reward"), "女巫 · 贵族"),
                ("1", "海妖之歌", npc_cell("vendor", "奈莎"), "贵族 · 圣堂武僧"),
            ]),
            poedb_slug="Ice_Nova",
        )
        self.assertEqual([r.source_kind for r in rows], ["quest_reward", "vendor"])
        self.assertIsNone(rows[0].npc_zh)
        self.assertEqual(rows[1].npc_zh, "奈莎")
        self.assertEqual(rows[0].act, 1)
        self.assertEqual(rows[0].quest_zh, "海妖之歌")

    def test_起始箱靠图标认而不是靠文本(self) -> None:
        """**这一条是这个抽取器最容易写错的地方。**

        起始箱那 21 行 NPC 单元格**是空的**（章节 1「绝望岩滩」，七个职业的开局
        技能火球 / 重击 / 灵体投掷…）。按单元格文本判会把这 21 行静默丢掉，
        而丢掉的正是「新角色一开始拿什么技能」这个最常见的问题。
        """
        rows = parse_gem_quests(
            quest_page([("1", "绝望岩滩", npc_cell("box"), "女巫")]),
            poedb_slug="Fireball",
        )
        self.assertEqual(rows[0].source_kind, "box")
        self.assertIsNone(rows[0].npc_zh)
        self.assertEqual(rows[0].classes, ("女巫",))

    def test_上游漏了图标时回退取文本(self) -> None:
        """实测 4 行（元素要害 / 能量之刃 / 衰弱 / 时空锁链，都在「迷失的爱情」）
        没有 `<i>`，但单元格文本仍写着「任务奖励」。"""
        rows = parse_gem_quests(
            quest_page([("3", "迷失的爱情", npc_cell("no_icon"), "暗影")]),
            poedb_slug="Enfeeble",
        )
        self.assertEqual(rows[0].source_kind, "quest_reward")

    def test_全职业展开成七个(self) -> None:
        rows = parse_gem_quests(
            quest_page([("4", "永恒梦魇", npc_cell("quest_reward"), "全职业")]),
            poedb_slug="Chain_Support",
        )
        self.assertEqual(rows[0].classes, CLASSES)
        self.assertEqual(len(CLASSES), 7)
        # 原文留着：用户就是拿「全职业」这个词问的。
        self.assertEqual(rows[0].classes_raw, "全职业")

    def test_同一格里重复的职业要去重(self) -> None:
        """实测「能量之刃」写成 `暗影 · 暗影`。不去重会让「哪些职业能拿」数出重复项。"""
        rows = parse_gem_quests(
            quest_page([("3", "迷失的爱情", npc_cell("quest_reward"), "暗影 · 暗影")]),
            poedb_slug="Energy_Blade",
        )
        self.assertEqual(rows[0].classes, ("暗影",))

    def test_按表头定位而不是按下标取表(self) -> None:
        page = quest_page(
            [("1", "医者之心", npc_cell("quest_reward"), "游侠")],
            before='<table><thead><tr><th>品质</th><th>效果</th></tr></thead>'
                   "<tbody><tr><td>20</td><td>x</td></tr></tbody></table>",
        )
        rows = parse_gem_quests(page, poedb_slug="Burning_Arrow")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].quest_zh, "医者之心")

    def test_没有使命表返回空而不是报错(self) -> None:
        """152 颗（瓦尔、觉醒辅助、契约类）页面上根本没有这张表，
        那是**数据缺口**而不是结构错误。"""
        page = "<table><thead><tr><th>等级</th><th>需求等级</th></tr></thead></table>"
        self.assertEqual(parse_gem_quests(page, poedb_slug="Vaal_Fireball"), ())

    def test_未知的途径标记直接报错(self) -> None:
        """归到「其它」会让分类错了没人看得出来——与逐级表那份白名单同一条纪律。"""
        page = quest_page([("1", "医者之心", '<i title="拍卖行"></i>', "游侠")])
        with self.assertRaises(GemPageError):
            parse_gem_quests(page, poedb_slug="X")

    def test_列数不对直接报错(self) -> None:
        page = (
            f"<table><thead>{_HEAD}</thead><tbody>"
            "<tr><td>1</td><td>医者之心</td></tr></tbody></table>"
        )
        with self.assertRaises(GemPageError):
            parse_gem_quests(page, poedb_slug="X")


class MergeTest(unittest.TestCase):
    """同一个主键下两行必须**取职业并集**。"""

    def _row(self, kind: str, npc: str | None, classes: tuple[str, ...]) -> GemQuestRow:
        return GemQuestRow(
            act=1, quest_zh="医者之心", quest_slug="Mercy_Mission",
            source_kind=kind, npc_zh=npc, classes=classes,
            classes_raw=" · ".join(classes),
        )

    def test_职业不相交的两行要合并而不是丢一行(self) -> None:
        """实测「元素扩散(辅)」：奈莎在同一个使命下卖给 `贵族·暗影·女巫`
        与 `圣堂武僧` 两行。丢掉一行就会少掉圣堂武僧——而那正是用户要问的
        「我这个职业能不能拿」。"""
        merged = merge_rows((
            self._row("vendor", "奈莎", ("贵族", "暗影", "女巫")),
            self._row("vendor", "奈莎", ("圣堂武僧",)),
        ))
        self.assertEqual(len(merged), 1)
        self.assertEqual(set(merged[0].classes), {"贵族", "暗影", "女巫", "圣堂武僧"})

    def test_同一批职业换个顺序算重复(self) -> None:
        """实测三处（元素要害 / 衰弱 / 时空锁链）——正是上游漏了图标又重列一遍的那几行。"""
        merged = merge_rows((
            self._row("quest_reward", None, ("女巫", "贵族", "暗影")),
            self._row("quest_reward", None, ("贵族", "暗影", "女巫")),
        ))
        self.assertEqual(len(merged), 1)
        self.assertEqual(set(merged[0].classes), {"女巫", "贵族", "暗影"})

    def test_任务奖励与商人是两条途径不合并(self) -> None:
        merged = merge_rows((
            self._row("quest_reward", None, ("女巫",)),
            self._row("vendor", "奈莎", ("女巫",)),
        ))
        self.assertEqual(len(merged), 2)


class CardTest(unittest.TestCase):
    """卡片必须真的写出来——「入库但没有出口」是这个项目踩过三次的坑。"""

    def _card(self, quests: list[dict], *, loaded: bool = True) -> str:
        from tests.unit.test_pob_skills import _entity as build

        entity = build(
            [{"gem_level": 1, "level_requirement": 12}],
            quests=quests, quests_loaded=loaded,
        )
        return render_skills.render_card(entity)

    def test_三种途径各自的说法(self) -> None:
        card = self._card([
            {"act": 1, "quest_zh": "海妖之歌", "source_kind": "quest_reward",
             "npc_zh": None, "classes": ["女巫", "贵族"]},
            {"act": 1, "quest_zh": "海妖之歌", "source_kind": "vendor",
             "npc_zh": "奈莎", "classes": ["贵族"]},
            {"act": 1, "quest_zh": "绝望岩滩", "source_kind": "box",
             "npc_zh": None, "classes": ["女巫"]},
        ])
        self.assertIn("**获取途径**", card)
        self.assertIn("第 1 章「海妖之歌」任务奖励（女巫、贵族）", card)
        self.assertIn("第 1 章「海妖之歌」向奈莎购买（贵族）", card)
        self.assertIn("第 1 章「绝望岩滩」开局箱子（女巫）", card)

    def test_没有使命表时写中性事实而不是推断掉落(self) -> None:
        """那 152 颗靠掉落，但**页面没这么说**。写「所以靠掉落」就是经验推断，
        正是第一原则要拦的东西（与通货那 15 条没有效果文本时「留空而不自己补一句」
        是同一条规矩）。"""
        card = self._card([])
        self.assertIn("POEDB 详情页未给出任务/商人表", card)
        self.assertIn("本库未采集该宝石的获取途径", card)
        self.assertNotIn("掉落", card)

    def test_这一维没导入过时整块不出现(self) -> None:
        """整张表是空的说明 `import_gem_quests.py` 还没跑过。这时写
        「POEDB 详情页没给」是一句**假话**——页面明明给了。"""
        self.assertNotIn("获取途径", self._card([], loaded=False))

    def test_超过上限要写出还剩多少(self) -> None:
        """静默截断等同于漏答（与效果文本、`mod_tiers` 同一条约定）。"""
        rows = [
            {"act": 1, "quest_zh": f"使命{i}", "source_kind": "quest_reward",
             "npc_zh": None, "classes": ["女巫"]}
            for i in range(render_skills.MAX_CARD_QUEST_LINES + 3)
        ]
        self.assertIn("另有 3 条获取途径", self._card(rows))


if __name__ == "__main__":
    unittest.main()


# ── 技能 × 辅助的搭配关系（第四十五轮）────────────────────────


def support_block(label: str, slugs: list[str], *, pane: str | None,
                  declared: int | None = None) -> str:
    """一个「被辅助 / 可辅助」区块。每个条目在页面上是**两个链接**（图标 + 文字）。"""
    body = "".join(
        f'<a href="/cn/{s}"><img/></a><a href="/cn/{s}">{s}</a>' for s in slugs)
    n = declared if declared is not None else len(slugs)
    card = f'<div class="card"><h5 class="card-header">{label} /{n} </h5>{body}</div>'
    if pane is None:
        return card
    return f'<div class="tab-pane fade" id="{pane}">{card}</div>'


class SupportLinksTest(unittest.TestCase):
    def _parse(self, html: str, slug: str = "Freezing_Pulse"):
        from kb.sources.poedb_gem_page import parse_gem_support_links

        return parse_gem_support_links(html, poedb_slug=slug)

    def test_两个方向分别取出(self) -> None:
        html = (support_block("被辅助", ["A_Support", "B_Support"], pane="被辅助")
                + support_block("可辅助", ["Fireball"], pane="可辅助"))
        got = self._parse(html)
        self.assertEqual(got.supported_by, ("A_Support", "B_Support"))
        self.assertEqual(got.can_support, ("Fireball",))

    def test_按标签页id选块而不是按位置(self) -> None:
        """一页上可能有好几个「被辅助」，分别属于**同一页上的不同技能**。
        实测 272 页有两个、其中 59 页内容不同（冰封军团 14 vs 118 颗）。
        取错块 = 把另一个技能的清单挂到这颗宝石上。"""
        html = (support_block("被辅助", ["Other_Support"], pane="WeaponPassive")
                + support_block("被辅助", ["Mine_Support"], pane="被辅助"))
        self.assertEqual(self._parse(html).supported_by, ("Mine_Support",))

    def test_没有同名标签页时取空而不是退而求其次(self) -> None:
        """实测 19 颗辅助宝石只有一个区块，而它在**这颗辅助所赋予的技能**
        那个标签页里（裂隙虫群（辅）→ SummonHiveborn）——那份清单描述的是
        被赋予的技能，不是这颗辅助本身。「只有一个就用它」会把 19 份清单挂错对象。"""
        html = support_block("被辅助", ["X_Support"], pane="SummonHiveborn")
        self.assertEqual(self._parse(html).supported_by, ())

    def test_声明条数是自证判据(self) -> None:
        """标题里的 `/N` 按**条目**算，而每条目有两个链接。"""
        html = support_block("被辅助", ["A_Support"], pane="被辅助", declared=9)
        with self.assertRaises(GemPageError):
            self._parse(html)

    def test_两个条目共用一张详情页不算错(self) -> None:
        """实测 24 个区块去重后少 1，成因全是两个条目共用一张页
        （`Convocation` 出现 4 次 = 2 条目 × 2 链接）。判据按**链接对数**，
        所以这里声明 2、去重后 1，仍应通过。"""
        html = ('<div class="tab-pane" id="被辅助"><div class="card">'
                '<h5 class="card-header">被辅助 /2 </h5>'
                '<a href="/cn/Convocation"><img/></a><a href="/cn/Convocation">a</a>'
                '<a href="/cn/Convocation"><img/></a><a href="/cn/Convocation">b</a>'
                "</div></div>")
        self.assertEqual(self._parse(html).supported_by, ("Convocation",))

    def test_没有区块返回空(self) -> None:
        got = self._parse("<div>没有搭配区块</div>")
        self.assertEqual((got.supported_by, got.can_support), ((), ()))


class SupportRouteTest(unittest.TestCase):
    """搭配问法的路由。**判据是问句形式，不是话题词。**"""

    def route(self, question: str) -> str:
        from orchestrator.loop import _preferred_tool_route

        return _preferred_tool_route(question)

    def test_搭配问法路由到搭配工具(self) -> None:
        for q in ("多重投射能不能辅助冰霜脉冲", "冰霜脉冲能配哪些辅助",
                  "闪电箭可以辅助吗", "重击能用哪些辅助"):
            with self.subTest(q=q):
                self.assertEqual("gem_support_match", self.route(q))

    def test_逐级数值问法不被搭配工具抢走(self) -> None:
        """**这一条是加意图词时最容易犯的错**：裸的「辅助」会把某颗辅助宝石的
        逐级数值问题整片抢走。与第二十一轮「法术压制」被 mitigation_calc 抢、
        第三十三轮「命中率」让给 accuracy_calc 是同一个形状。

        断言写成「不是 gem_support_match」而不是「是 skill_gem_levels」：
        `skill_gem_levels` 本来就不在偏好工具表里（由模型自选），
        断言它会是在钉一个系统从未承诺过的行为——第一版黄金用例正是这么写错的。
        """
        for q in ("多重投射辅助 20 级的消耗与保留倍率是多少",
                  "启迪辅助的冷却时间", "多重投射(辅) 是什么颜色"):
            with self.subTest(q=q):
                self.assertNotEqual("gem_support_match", self.route(q))
