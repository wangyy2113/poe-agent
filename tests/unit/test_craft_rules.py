"""前后缀名额规则：两侧抽取与对账（2026-08-27 第三十七轮）。

这一维**不是「只有 PoB 说得清」**——POEDB 词缀页用四条 `<li>` 明说了规则，
PoB `Classes/Item.lua` 是同一套规则的代码形态。魔法 1+1 与稀有 3+3 两侧都说，
那就是对账口径；不重叠的各自标记，**单侧来源不是冲突**。

PoB 那侧刻意用**锚点正则**而不是求值控制流：那是代码不是数据表，而项目硬规则
说不重算 PoB 引擎。上游改写导致锚点失配时必须**报错**，不静默沿用旧值——
数据漂移有 `check_doc_facts.py` 拦，公式/规则漂移只能靠钉原文。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kb.sources.poedb_modifiers import (  # noqa: E402
    ModifiersStructureError,
    extract_affix_limits,
)
from kb.sources.pob_affix_limit import (  # noqa: E402
    AffixLimitSourceError,
    parse_affix_limits,
)

#: POEDB 词缀页的四条规则，逐字照抄页面（2026-08-27 抓取）。
_POEDB_HTML = """
<html><body><ul>
<li>指在前缀生成的类型。</li>
<li><span class="item_magic">魔法物品</span>最多一随机前缀一随机后缀</li>
<li><span class="item_rare">稀有物品</span>最多三随机前缀三随机后缀</li>
<li><span class="item_unique">传奇物品</span>不受前后缀的数量限制</li>
<li>药剂因为只有魔法物品, 所以最多一随机前缀一随机后缀</li>
<li><span class="item_magic">部分物品会有固定词缀, 固定词缀不受前后缀的数量限制</span></li>
</ul></body></html>
"""

#: `Classes/Item.lua` 的相关片段，逐字照抄上游（PoB 2.66.2）。
_POB_LUA = '''
	self.affixLimit = 0
	if self.crafted then
		if not self.affixes then
			self.crafted = false
		elseif self.rarity == "MAGIC" then
			if self.prefixes.limit or self.suffixes.limit then
				self.prefixes.limit = m_max(m_min((self.prefixes.limit or 0) + 1, 2), 0)
				self.suffixes.limit = m_max(m_min((self.suffixes.limit or 0) + 1, 2), 0)
				self.affixLimit = self.prefixes.limit + self.suffixes.limit
			else
				self.affixLimit = 2
			end
		elseif self.rarity == "RARE" then
			self.affixLimit = (((self.type == "Jewel" and not (self.base.subType == "Abyss" and self.corrupted)) or self.type == "Graft") and 4 or 6)
		end
	end
'''


def _by_key(rules) -> dict[tuple[str, str], object]:  # noqa: ANN001
    return {(r.rarity, r.item_kind): r for r in rules}


class PoedbRuleTest(unittest.TestCase):
    def test_抽出四条规则(self) -> None:
        rules = _by_key(extract_affix_limits(_POEDB_HTML))
        self.assertEqual((1, 1), _limits(rules[("魔法", "default")]))
        self.assertEqual((3, 3), _limits(rules[("稀有", "default")]))
        self.assertEqual((1, 1), _limits(rules[("魔法", "flask")]))

    def test_传奇是不受限制而不是没查到(self) -> None:
        """`prefix_limit is None` 表示「不受限制」；没查到的规则根本不会有行。

        判定时混淆这两件事，「传奇不受限制」就会被读成「规则缺失 → unknown」——
        虽然结论都是 unknown，但给用户的理由完全不同。
        """
        rule = _by_key(extract_affix_limits(_POEDB_HTML))[("传奇", "default")]
        self.assertTrue(rule.unlimited)
        self.assertIsNone(rule.prefix_limit)

    def test_措辞改了要报错而不是少一条(self) -> None:
        broken = _POEDB_HTML.replace("稀有物品</span>最多三随机前缀三随机后缀", "稀有物品</span>的名额另有规定")
        with self.assertRaisesRegex(ModifiersStructureError, "稀有"):
            extract_affix_limits(broken)


def _limits(rule) -> tuple[int | None, int | None]:  # noqa: ANN001
    return (rule.prefix_limit, rule.suffix_limit)


class PoBRuleTest(unittest.TestCase):
    def test_魔法与稀有的默认名额(self) -> None:
        rules = _by_key(parse_affix_limits(_POB_LUA))
        self.assertEqual((1, 1), _limits(rules[("魔法", "default")]))
        self.assertEqual((3, 3), _limits(rules[("稀有", "default")]))

    def test_珠宝与graft是2加2(self) -> None:
        rules = _by_key(parse_affix_limits(_POB_LUA))
        self.assertEqual((2, 2), _limits(rules[("稀有", "jewel")]))
        self.assertEqual((2, 2), _limits(rules[("稀有", "graft")]))

    def test_已腐化的深渊珠宝回到3加3(self) -> None:
        """反直觉的一条：判据里 `not (subType == "Abyss" and corrupted)`
        把这一支排除在 4 之外，所以它走默认的 6（3+3）。最容易写反，单独钉住。
        """
        rules = _by_key(parse_affix_limits(_POB_LUA))
        self.assertEqual((3, 3), _limits(rules[("稀有", "jewel_abyss_corrupted")]))

    def test_上游锚点失配必须报错(self) -> None:
        broken = _POB_LUA.replace('self.rarity == "RARE"', 'self.rarity == "RAREX"')
        with self.assertRaisesRegex(AffixLimitSourceError, "RARE"):
            parse_affix_limits(broken)

    def test_affixLimit为奇数时报错(self) -> None:
        """PoB 在别处按 `affixLimit / 2` 平分到前后缀，奇数说明读错了字面量。"""
        broken = _POB_LUA.replace("and 4 or 6)", "and 4 or 7)")
        with self.assertRaisesRegex(AffixLimitSourceError, "偶数"):
            parse_affix_limits(broken)


class ReconcileTest(unittest.TestCase):
    def test_重叠的两条两侧一致(self) -> None:
        """这就是这一维的对账口径：只有两边都说的才比得了。"""
        poedb = _by_key(extract_affix_limits(_POEDB_HTML))
        pob = _by_key(parse_affix_limits(_POB_LUA))
        overlap = set(poedb) & set(pob)
        self.assertEqual({("魔法", "default"), ("稀有", "default")}, overlap)
        for key in overlap:
            self.assertEqual(_limits(poedb[key]), _limits(pob[key]), key)

    def test_单侧来源不是冲突(self) -> None:
        poedb = _by_key(extract_affix_limits(_POEDB_HTML))
        pob = _by_key(parse_affix_limits(_POB_LUA))
        # 传奇与药剂只有 POEDB 说；珠宝/Graft/腐化深渊珠宝只有 PoB 说
        self.assertEqual({("传奇", "default"), ("魔法", "flask")}, set(poedb) - set(pob))
        self.assertEqual(
            {("稀有", "jewel"), ("稀有", "graft"), ("稀有", "jewel_abyss_corrupted")},
            set(pob) - set(poedb),
        )


if __name__ == "__main__":
    unittest.main()
