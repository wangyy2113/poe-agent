"""装备文本解析器（任务 4.1，需求 7.1/7.2/7.6）。

游戏里 Ctrl+C 复制出来的文本以 `--------` 分段，段落语义由位置与关键字共同决定。
本模块把它解析成 `ParsedItem`，**不做任何数据库查询**——T 级反查在 `item_mods.py`。

## 格式依据

英文格式不凭记忆，取自工作区内 Path of Building 社区版的解析器实现
（`src/Classes/Item.lua`，MIT）：段落分隔符、旗标行、影响词行 `{名称} Item`、
词缀行的小写括号后缀集合、提示文本以 `(` 开头且到 `)` 结束、底材名前缀
`Superior/Synthesised/Vestigial`。样例文本取自同仓库 `spec/System/TestItemParse_spec.lua`。

## 两种复制格式

1. **普通复制（Ctrl+C）**：`+142 to maximum Life`
2. **进阶复制（Ctrl+Alt+C）**：多一行 `{ Prefix Modifier "Fecund" (Tier: 1) — Life }`
   头，且数值带区间 `+142(130-144) to maximum Life`

进阶复制里**游戏自己给出了 T 级与区间**，那是比查我们的库更硬的证据——直接采信，
不再反查。这也顺带绕开了「POEDB 的 T 级只有中文侧」这个限制：英文客户端用
Ctrl+Alt+C 一样能拿到 T 级。

## 中文标签的诚实说明

英文标签有 PoB 的实现作依据。**中文客户端的表头标签没有可核实的来源**
（POEDB 缓存页里只有词缀文本，没有物品提示框的标签），所以 `_ZH_KEYS` 是待核实的。
代码结构保证「标签猜错只会导致该字段留空并进 `warnings`，不会静默错解析」：
未识别的 `键: 值` 一律原样收进 `properties` 并记告警，绝不按位置硬认。

## 失败契约（需求 7.6）

认不出就抛 `ItemParseError`，带明确原因与期望格式样例，**不猜测续行**。
宁可让用户重贴一次，也不能把半个解析结果当完整装备去分析——那会让后续所有
结论建立在残缺前提上，而用户看不出哪里缺了。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from entities.taxonomy import ModKind

#: 多行词缀的行间分隔符，与 `kb/sources/poedb_mods.py` 的 LINE_SEP 保持一致。
#: 库里混合词缀存成「+(4-6)% 所有元素抗性 / 召唤生物获得 +(5-8)% 元素抗性」，
#: 装备上却是两行；反查前必须按同样的分隔符拼回去才可能匹配上。
LINE_SEP = " / "

_SECTION_RE = re.compile(r"^-{3,}$")
#: `键: 值`。键允许空格与括号（`Quality (Cold Modifiers)`），但不含冒号。
_PROP_RE = re.compile(r"^([^:：]{1,40})[:：]\s*(.*)$")
#: 词缀行尾的小写括号旗标，如 ` (implicit)`。取自 PoB 的 lineFlags。
_FLAG_SUFFIX_RE = re.compile(r"\s*\(([a-z]+)\)\s*$")
#: 进阶复制的词缀头：`{ Prefix Modifier "Fecund" (Tier: 1) — Life }`
_ADVANCED_HEADER_RE = re.compile(r"^\{\s*(.+?)\s*\}$")
#: 进阶复制的数值形态：`142(130-144)` → 实际值 142，区间 130-144
_VALUE_WITH_RANGE_RE = re.compile(r"(-?\d+(?:\.\d+)?)\((-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)\)")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
#: 提示文本（reminder text）起始：`(Armour, Evasion...` 或 `(20% of ...`
_REMINDER_RE = re.compile(r"^\((?:[A-Za-z\u4e00-\u9fff]|\d+%? of )")


class Rarity(StrEnum):
    NORMAL = "普通"
    MAGIC = "魔法"
    RARE = "稀有"
    UNIQUE = "传奇"
    RELIC = "遗物"
    UNKNOWN = "未知"


#: 英文稀有度 → 枚举。依据 PoB `Item.lua`：`Rarity: (%a+)` 后大写比对。
_EN_RARITY = {
    "normal": Rarity.NORMAL,
    "magic": Rarity.MAGIC,
    "rare": Rarity.RARE,
    "unique": Rarity.UNIQUE,
    "relic": Rarity.RELIC,
}
#: 中文稀有度。「传奇」有 P1 数据佐证（POEDB 唯一装备页的 pane 值形如 "Weapon传奇"），
#: 其余三项按同一套官方译名推定，**待真机核实**。
_ZH_RARITY = {
    "普通": Rarity.NORMAL,
    "魔法": Rarity.MAGIC,
    "稀有": Rarity.RARE,
    "传奇": Rarity.UNIQUE,
    "傳奇": Rarity.UNIQUE,
    "遗物": Rarity.RELIC,
    "遺物": Rarity.RELIC,
}

#: 词缀行后缀旗标 → 词缀类型。PoB 的 lineFlags 是全集，这里只映射与
#: 需求 7.2 相关的那些；其余（prefix/suffix/disabled/custom…）只作标记不改类型。
_FLAG_TO_KIND = {
    "implicit": ModKind.IMPLICIT,
    "enchant": ModKind.ENCHANT,
    "crafted": ModKind.CRAFTED,
    "fractured": ModKind.FRACTURED,
    "synthesis": ModKind.SYNTHESISED,
    "veiled": ModKind.CRAFTED,
    "unveiled": ModKind.CRAFTED,
}
#: 已知但不改变词缀类型的旗标。列出来是为了区分「认识但无关」与「完全不认识」——
#: 后者要进 warnings，前者不该打扰用户。
_KNOWN_NEUTRAL_FLAGS = {
    "crucible", "custom", "disabled", "eater", "exarch", "scourge",
    "mutated", "unscalable", "prefix", "suffix", "vestigial",
}

#: 独立成行的旗标。英文依据 PoB `Item.lua` 的字面比对分支。
_EN_FLAG_LINES = {
    "corrupted": "corrupted",
    "mirrored": "mirrored",
    "split": "split",
    "unidentified": "unidentified",
    "fractured item": "fractured",
    "synthesised item": "synthesised",
}
#: 隐匿词缀占位行。游戏里未解锁的隐匿词缀显示为「Veiled Prefix」而非词缀文本，
#: 它不是词缀内容，当成词缀会得到一条永远匹配不上的垃圾数据。依据 PoB `Item.lua`。
_VEILED_LINES = {"veiled prefix", "veiled suffix"}

#: 中文旗标行。「已腐化」有 P1 佐证（mod_affix 里 category='corrupted' 的
#: category_label 就是「已腐化」），其余待核实。
_ZH_FLAG_LINES = {
    "已腐化": "corrupted",
    "已镜像": "mirrored",
    "已鏡像": "mirrored",
    "已分裂": "split",
    "未鉴定": "unidentified",
    "未鑑定": "unidentified",
    "裂隙物品": "fractured",
    "合成物品": "synthesised",
    "虚空忆境物品": "synthesised",
}

#: 影响词行。英文取自 PoB `ItemTools.lua` 的 influenceInfo.all 的 display 字段；
#: 中文取自 mod_affix.category_label（P1 POEDB 数据，可核实）。
_INFLUENCE_DISPLAY = {
    "shaper": ("Shaper", "塑界者"),
    "elder": ("Elder", "裂界者"),
    "warlord": ("Warlord", "督军"),
    "hunter": ("Hunter", "狩猎者"),
    "crusader": ("Crusader", "圣战者"),
    "redeemer": ("Redeemer", "救赎者"),
    "searing_exarch": ("Searing Exarch", "焚界者"),
    "eater_of_worlds": ("Eater of Worlds", "灭界者"),
}
_EN_INFLUENCE_LINES = {f"{en.lower()} item": key for key, (en, _) in _INFLUENCE_DISPLAY.items()}
_ZH_INFLUENCE_LINES = {f"{zh}物品": key for key, (_, zh) in _INFLUENCE_DISPLAY.items()}

#: 属性键 → 规范名。英文键依据 PoB 的 specName 分支。
_EN_KEYS = {
    "item class": "item_class",
    "rarity": "rarity",
    "item level": "item_level",
    "quality": "quality",
    "sockets": "sockets",
    "requirements": "requirements",
    "level": "req_level",
    "str": "req_str",
    "dex": "req_dex",
    "int": "req_int",
    "strength": "req_str",
    "dexterity": "req_dex",
    "intelligence": "req_int",
    "requires level": "req_level",
    "armour": "armour",
    "evasion rating": "evasion",
    "energy shield": "energy_shield",
    "ward": "ward",
    "physical damage": "physical_damage",
    "elemental damage": "elemental_damage",
    "chaos damage": "chaos_damage",
    "critical strike chance": "crit_chance",
    "attacks per second": "attack_speed",
    "weapon range": "weapon_range",
    "block chance": "block_chance",
    "chance to block": "block_chance",
    "limited to": "limited_to",
    "radius": "radius",
    "talisman tier": "talisman_tier",
    "stack size": "stack_size",
    "note": "note",
    "memory strands": "memory_strands",
}
#: 中文属性键。**整表待真机核实**（见模块 docstring）。认错的后果被限制为
#: 「该字段留空 + 进 warnings」，不会静默错解析。
_ZH_KEYS = {
    "物品类别": "item_class",
    "物品類別": "item_class",
    "稀有度": "rarity",
    "物品等级": "item_level",
    "物品等級": "item_level",
    "品质": "quality",
    "品質": "quality",
    "插槽": "sockets",
    "需求": "requirements",
    "等级": "req_level",
    "等級": "req_level",
    "力量": "req_str",
    "敏捷": "req_dex",
    "智慧": "req_int",
    "智力": "req_int",
    "护甲": "armour",
    "護甲": "armour",
    "闪避值": "evasion",
    "閃避值": "evasion",
    "能量护盾": "energy_shield",
    "能量護盾": "energy_shield",
    "结界": "ward",
    "物理伤害": "physical_damage",
    "物理傷害": "physical_damage",
    "元素伤害": "elemental_damage",
    "元素傷害": "elemental_damage",
    "混沌伤害": "chaos_damage",
    "暴击率": "crit_chance",
    "暴擊率": "crit_chance",
    "每秒攻击次数": "attack_speed",
    "每秒攻擊次數": "attack_speed",
    "武器范围": "weapon_range",
    "格挡值": "block_chance",
    "格擋值": "block_chance",
    "限制": "limited_to",
    "范围": "radius",
    "堆叠数量": "stack_size",
    "备注": "note",
    "備註": "note",
}

#: 期望格式样例，解析失败时回给用户（需求 7.6）。取自真实游戏复制格式。
EXPECTED_SAMPLE = """Item Class: Gloves
Rarity: Rare
Havoc Grip
Fugitive Gloves
--------
Quality: +20% (augmented)
Evasion Rating: 220 (augmented)
--------
Requirements:
Level: 70
Dex: 51
Int: 51
--------
Sockets: R-G-B-B
--------
Item Level: 84
--------
+12% to Chaos Resistance (implicit)
--------
+45 to maximum Life
+35% to Fire Resistance
--------
Corrupted"""

FAILURE_HINT = (
    "请在游戏中把鼠标悬停在装备上按 Ctrl+C（进阶复制 Ctrl+Alt+C 会额外带上 T 级与"
    "词缀区间，更准），然后原样粘贴，不要手改或删行。"
)


class ItemParseError(ValueError):
    """解析失败。带原因与期望格式样例，不做部分猜测（需求 7.6）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.expected_sample = EXPECTED_SAMPLE
        self.hint = FAILURE_HINT


@dataclass
class SocketLayout:
    """插槽与连接。`groups` 的每一项是一个相连组的颜色序列。"""

    raw: str = ""
    groups: list[list[str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(len(g) for g in self.groups)

    @property
    def max_links(self) -> int:
        return max((len(g) for g in self.groups), default=0)

    def render(self) -> str:
        return " ".join("-".join(g) for g in self.groups)


@dataclass
class ItemFlags:
    corrupted: bool = False
    mirrored: bool = False
    split: bool = False
    unidentified: bool = False
    synthesised: bool = False
    fractured: bool = False
    #: 有未解锁的隐匿词缀
    veiled: bool = False
    #: 影响词键，如 shaper/elder/searing_exarch
    influences: list[str] = field(default_factory=list)

    def labels(self) -> list[str]:
        out = []
        if self.corrupted:
            out.append("已腐化")
        if self.veiled:
            out.append("含未解锁隐匿词缀")
        if self.mirrored:
            out.append("已镜像")
        if self.split:
            out.append("已分裂")
        if self.unidentified:
            out.append("未鉴定")
        if self.synthesised:
            out.append("合成")
        if self.fractured:
            out.append("含裂隙词缀")
        out.extend(_INFLUENCE_DISPLAY[k][1] for k in self.influences if k in _INFLUENCE_DISPLAY)
        return out


@dataclass
class Requirements:
    level: int | None = None
    strength: int | None = None
    dexterity: int | None = None
    intelligence: int | None = None


@dataclass
class ParsedMod:
    """装备上的一条词缀。

    `text` 是**归一化后**的展示文本：进阶复制的 `142(130-144)` 会被还原成 `142`，
    这样它与普通复制的形态一致，反查时走同一条路径。原文留在 `raw`。

    `values` 是这条词缀上的实际数值，按出现顺序。`ranges` 只在进阶复制时有值——
    那是游戏给出的该词缀在此装备上的区间，可直接算 roll 百分比。

    `tier` 同理：只有进阶复制会带。**普通复制时它是 None，绝不填猜测值**，
    留给 `item_mods.py` 去查库，查不到就如实说未匹配（需求 7.9）。
    """

    kind: ModKind
    text: str
    raw: str = ""
    values: list[float] = field(default_factory=list)
    ranges: list[tuple[float, float]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    #: 进阶复制头里的信息
    affix_type: str = ""          # Prefix / Suffix
    affix_name: str = ""          # "Fecund"
    tier: int | None = None
    tier_rank: int | None = None  # 工艺台词缀用 Rank 而非 Tier
    tags: list[str] = field(default_factory=list)
    #: 该词缀在装备上占了几行（混合词缀占多行）。反查时要按 LINE_SEP 拼起来。
    lines: list[str] = field(default_factory=list)
    section: int = -1

    @property
    def is_multiline(self) -> bool:
        return len(self.lines) > 1

    @property
    def lookup_text(self) -> str:
        """反查用文本：多行词缀按库里的分隔符拼回一行。"""
        return LINE_SEP.join(self.lines) if len(self.lines) > 1 else self.text

    def roll_pct(self) -> float | None:
        """实际数值在区间内的位置（0~1）。需要进阶复制的区间，否则 None。

        多个数值位时取各位的平均。区间退化为定值时该位不计入——
        定值位没有「roll 得好不好」这回事，计进去会把平均值稀释成假信息。
        """
        if not self.ranges or len(self.ranges) != len(self.values):
            return None
        pcts = [
            (v - lo) / (hi - lo)
            for v, (lo, hi) in zip(self.values, self.ranges, strict=True)
            if hi > lo
        ]
        if not pcts:
            return None
        return sum(pcts) / len(pcts)


@dataclass
class ParsedItem:
    """解析结果。

    刻意把「没解析出来的东西」也留在结构里（`properties` 里的未知键、
    `unclassified`、`warnings`），而不是丢掉：丢掉之后调用方无法知道自己
    拿到的是完整装备还是残件。
    """

    language: str = "en"          # en / zh
    advanced_copy: bool = False   # 是否 Ctrl+Alt+C 的进阶复制
    item_class: str = ""
    rarity: Rarity = Rarity.UNKNOWN
    name: str = ""
    base_type: str = ""
    item_level: int | None = None
    quality: int | None = None
    sockets: SocketLayout = field(default_factory=SocketLayout)
    requirements: Requirements = field(default_factory=Requirements)
    flags: ItemFlags = field(default_factory=ItemFlags)

    implicits: list[ParsedMod] = field(default_factory=list)
    explicits: list[ParsedMod] = field(default_factory=list)
    enchants: list[ParsedMod] = field(default_factory=list)

    #: 原样保留的属性行（含未识别的键），键为规范名或原始键
    properties: dict[str, str] = field(default_factory=dict)
    #: 属性段里的说明性裸行，如武器段开头的「Sceptre」。不是词缀
    info_lines: list[str] = field(default_factory=list)
    #: 无法归类的行。**不当成词缀**，如实报告
    unclassified: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def all_mods(self) -> list[ParsedMod]:
        return [*self.enchants, *self.implicits, *self.explicits]

    @property
    def crafted(self) -> list[ParsedMod]:
        return [m for m in self.explicits if m.kind is ModKind.CRAFTED]

    @property
    def fractured(self) -> list[ParsedMod]:
        return [m for m in self.all_mods if m.kind is ModKind.FRACTURED]

    @property
    def display_name(self) -> str:
        if self.name and self.base_type and self.name != self.base_type:
            return f"{self.name}（{self.base_type}）"
        return self.name or self.base_type


# ── 解析 ────────────────────────────────────────────────────


def _split_sections(text: str) -> list[list[str]]:
    """按 `--------` 分段。同时去掉每行的前后空白。

    去空白是必须的：从聊天软件、论坛、PoB 的测试文件里拷来的文本常带缩进，
    而游戏原文没有。不处理会让所有关键字比对失败。
    """
    sections: list[list[str]] = [[]]
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if _SECTION_RE.match(line):
            sections.append([])
            continue
        sections[-1].append(line)
    return [s for s in sections if s]


def _detect_language(sections: list[list[str]]) -> str:
    """按表头键是中文还是英文判定。只看键，不看值——值里本来就可能混英文。"""
    for section in sections[:3]:
        for line in section:
            m = _PROP_RE.match(line)
            if not m:
                continue
            key = m.group(1).strip()
            if key.lower() in _EN_KEYS:
                return "en"
            if key in _ZH_KEYS:
                return "zh"
    # 退路：整段文本里有 CJK 就当中文
    return "zh" if re.search(r"[\u4e00-\u9fff]", "\n".join("".join(s) for s in sections)) else "en"


def _norm_key(key: str, language: str) -> str | None:
    table = _ZH_KEYS if language == "zh" else _EN_KEYS
    hit = table.get(key) or table.get(key.lower())
    if hit:
        return hit
    other = _EN_KEYS if language == "zh" else _ZH_KEYS
    return other.get(key) or other.get(key.lower())


def _to_int(value: str) -> int | None:
    """从 `+20% (augmented)` 这种值里取整数。取不到返回 None，不给 0。

    返回 0 会让「没有品质」和「品质 0」变得无法区分，后者在分析里是不同的事实。
    """
    m = _NUM_RE.search(value)
    return int(float(m.group())) if m else None


def _parse_sockets(value: str) -> SocketLayout:
    """`R-G-B B-W` → [[R,G,B],[B,W]]。依据 PoB：空格分组，`-` 表示相连。"""
    layout = SocketLayout(raw=value.strip())
    for group in value.split():
        colors = [c for c in group.split("-") if c]
        if colors:
            layout.groups.append(colors)
    return layout


def _strip_flags(line: str) -> tuple[str, list[str], ModKind | None]:
    """剥掉行尾的 ` (implicit)` 之类旗标，返回 (正文, 旗标列表, 推出的类型)。

    可能有多个旗标叠加（`... (crafted) (fractured)`），逐个剥。
    """
    flags: list[str] = []
    kind: ModKind | None = None
    text = line
    while True:
        m = _FLAG_SUFFIX_RE.search(text)
        if not m:
            break
        flag = m.group(1)
        if flag not in _FLAG_TO_KIND and flag not in _KNOWN_NEUTRAL_FLAGS:
            break  # 不认识的括号后缀可能是词缀正文的一部分，不能剥
        flags.append(flag)
        if kind is None and flag in _FLAG_TO_KIND:
            kind = _FLAG_TO_KIND[flag]
        text = text[: m.start()].rstrip()
    flags.reverse()
    return text, flags, kind


def _extract_values(text: str) -> tuple[str, list[float], list[tuple[float, float]]]:
    """抽取数值。进阶复制的 `142(130-144)` 还原成 `142` 并记下区间。

    普通复制没有括号区间，`ranges` 为空——**不能拿数值本身当区间**，
    那会让「roll 满了」这种结论凭空出现。
    """
    ranges: list[tuple[float, float]] = []

    def repl(m: re.Match[str]) -> str:
        ranges.append((float(m.group(2)), float(m.group(3))))
        return m.group(1)

    normalized = _VALUE_WITH_RANGE_RE.sub(repl, text)
    values = [float(m.group()) for m in _NUM_RE.finditer(normalized)]
    return normalized, values, ranges


def _parse_advanced_header(inner: str) -> dict[str, object]:
    """解析进阶复制的 `{ ... }` 头。

    实测形态（取自 PoB 测试样本）：
        Prefix Modifier "Fecund" (Tier: 1) — Life
        Master Crafted Suffix Modifier "of Craft" (Rank: 3) — Elemental, Cold, Resistance
        Fractured Prefix Modifier "Thorny" (Tier: 2) — Damage, Physical
        Implicit Modifier — Damage, Critical  — 106% Increased
        Searing Exarch Implicit Modifier (Lesser)
        Unique Modifier

    这里只取能确定的字段。识别不出的部分留空，不推断。
    """
    info: dict[str, object] = {}
    head = inner
    tags: list[str] = []
    # 破折号后面是标签串（可能出现两次，第二次是「106% Increased」这类倍率说明）
    parts = re.split(r"\s+[—–]\s+", inner)
    if len(parts) > 1:
        head = parts[0]
        for chunk in parts[1:]:
            if re.match(r"^\d+%", chunk.strip()):
                continue  # 倍率说明不是标签
            tags.extend(t.strip() for t in chunk.split(",") if t.strip())
    if tags:
        info["tags"] = tags

    m = re.search(r"\(Tier:\s*(\d+)\)", head)
    if m:
        info["tier"] = int(m.group(1))
    m = re.search(r"\(Rank:\s*(\d+)\)", head)
    if m:
        info["tier_rank"] = int(m.group(1))
    m = re.search(r'"(.+?)"', head)
    if m:
        info["affix_name"] = m.group(1)
    if re.search(r"\bPrefix\b", head):
        info["affix_type"] = "Prefix"
    elif re.search(r"\bSuffix\b", head):
        info["affix_type"] = "Suffix"

    kind: ModKind | None = None
    if re.search(r"\bImplicit Modifier\b", head):
        kind = ModKind.IMPLICIT
    elif re.search(r"\bEnchant", head):
        kind = ModKind.ENCHANT
    if re.search(r"\bMaster Crafted\b", head):
        kind = ModKind.CRAFTED
    elif re.search(r"\bFractured\b", head):
        kind = ModKind.FRACTURED
    if kind is not None:
        info["kind"] = kind
    return info


def _is_reminder(line: str) -> bool:
    return bool(_REMINDER_RE.match(line))


def _classify_flag_line(line: str, language: str) -> tuple[str, str] | None:
    """判断是否为独立旗标行或影响词行。返回 ('flag'|'influence', 键)。"""
    key = line.lower().strip()
    for table, kind in ((_EN_FLAG_LINES, "flag"), (_EN_INFLUENCE_LINES, "influence")):
        if key in table:
            return (kind, table[key])
    bare = line.strip()
    if bare in _ZH_FLAG_LINES:
        return ("flag", _ZH_FLAG_LINES[bare])
    if bare in _ZH_INFLUENCE_LINES:
        return ("influence", _ZH_INFLUENCE_LINES[bare])
    del language  # 两种语言的表都查，客户端语言与物品语言可能不一致
    return None


def parse_item(text: str) -> ParsedItem:
    """解析装备文本。认不出就抛 `ItemParseError`（需求 7.6）。"""
    if not text or not text.strip():
        raise ItemParseError("输入为空")

    sections = _split_sections(text)
    if not sections:
        raise ItemParseError("文本里没有任何有效行")

    item = ParsedItem()
    item.language = _detect_language(sections)

    # ── 头部：Item Class / Rarity / 名称 / 底材 ──
    head = sections[0]
    cursor = 0
    for _ in range(2):  # 最多两行属性（Item Class、Rarity），顺序固定
        if cursor >= len(head):
            break
        m = _PROP_RE.match(head[cursor])
        if not m:
            break
        norm = _norm_key(m.group(1).strip(), item.language)
        value = m.group(2).strip()
        if norm == "item_class":
            item.item_class = value
        elif norm == "rarity":
            item.rarity = (
                _EN_RARITY.get(value.lower())
                or _ZH_RARITY.get(value)
                or Rarity.UNKNOWN
            )
            if item.rarity is Rarity.UNKNOWN:
                item.warnings.append(f"稀有度 {value!r} 不在已知取值内，未采信")
        else:
            break
        cursor += 1

    if item.rarity is Rarity.UNKNOWN and not item.item_class:
        raise ItemParseError(
            "开头既没有『Item Class:／物品类别:』也没有『Rarity:／稀有度:』行。"
            "这两行是游戏复制文本的固定开头，缺了说明这不是装备文本，或复制时被截断"
        )

    names = [ln for ln in head[cursor:] if not _PROP_RE.match(ln)]
    if names:
        item.name = names[0]
    if len(names) > 1:
        # 稀有/传奇是「名称 + 底材」两行；普通/魔法只有一行（含词缀前后缀）
        item.base_type = names[1]
    elif item.rarity in (Rarity.NORMAL, Rarity.MAGIC):
        item.base_type = item.name
    # 底材名前缀不是底材的一部分，依据 PoB：Superior/Synthesised/Vestigial
    for prefix in ("Superior ", "Synthesised ", "Vestigial ", "精良的 ", "合成 "):
        if item.base_type.startswith(prefix):
            item.base_type = item.base_type[len(prefix) :]

    if not item.name and not item.base_type:
        raise ItemParseError("解析不出物品名称与底材：开头段落里没有非属性行")

    # ── 其余段落 ──
    mod_sections: list[tuple[int, list[ParsedMod]]] = []
    for index, section in enumerate(sections[1:], start=1):
        mods = _parse_section(section, item, index)
        if mods:
            mod_sections.append((index, mods))

    _assign_mod_kinds(item, mod_sections)

    if not item.all_mods and item.rarity not in (Rarity.NORMAL,):
        item.warnings.append(
            "没解析出任何词缀。若装备确实有词缀，可能是复制文本被截断，建议重新复制"
        )
    return item


def _parse_section(section: list[str], item: ParsedItem, index: int) -> list[ParsedMod]:
    """解析一个段落，返回其中的词缀（属性/旗标直接写进 item）。

    **属性段里的裸行不是词缀。** 武器段开头有一行物品类型名（`Sceptre`）、
    药剂段有「Lasts 5 Seconds」，它们既不是 `键: 值` 也不是词缀。段内出现过
    已识别属性行就说明这是属性段，其中的裸行归 `info_lines`。
    不这么做的话「Sceptre」会变成一条显式词缀，然后带着整条分析一起错下去。
    """
    is_property_section = any(
        (m := _PROP_RE.match(line)) is not None and _looks_like_property(m, item.language)
        for line in section
    )
    mods: list[ParsedMod] = []
    pending_header: dict[str, object] | None = None
    i = 0
    while i < len(section):
        line = section[i]
        i += 1

        # 提示文本：以 `(` 开头，可能跨行，到 `)` 结束
        if _is_reminder(line):
            while not line.endswith(")") and i < len(section):
                line = section[i]
                i += 1
            continue

        m = _ADVANCED_HEADER_RE.match(line)
        if m:
            item.advanced_copy = True
            pending_header = _parse_advanced_header(m.group(1))
            continue

        if line.lower() in _VEILED_LINES:
            item.flags.veiled = True
            continue

        flag_hit = _classify_flag_line(line, item.language)
        if flag_hit:
            kind, key = flag_hit
            if kind == "flag":
                setattr(item.flags, key, True)
            elif key not in item.flags.influences:
                item.flags.influences.append(key)
            continue

        prop = _PROP_RE.match(line)
        if prop and _looks_like_property(prop, item.language):
            _apply_property(item, prop.group(1).strip(), prop.group(2).strip())
            continue

        if is_property_section:
            item.info_lines.append(line)
            continue

        # 剩下的按词缀处理
        body, flags, kind_from_flag = _strip_flags(line)
        normalized, values, ranges = _extract_values(body)
        header = pending_header or {}
        kind = kind_from_flag or header.get("kind") or ModKind.EXPLICIT
        mod = ParsedMod(
            kind=kind,  # type: ignore[arg-type]
            text=normalized,
            raw=line,
            values=values,
            ranges=ranges,
            flags=flags,
            lines=[normalized],
            section=index,
            affix_type=str(header.get("affix_type", "")),
            affix_name=str(header.get("affix_name", "")),
            tier=header.get("tier"),  # type: ignore[arg-type]
            tier_rank=header.get("tier_rank"),  # type: ignore[arg-type]
            tags=list(header.get("tags", [])),  # type: ignore[arg-type]
        )
        # 进阶复制里一个 `{ }` 头可能带多行（混合词缀）。同一个头下的后续行
        # 并进上一条，而不是当成新词缀——否则混合词缀会被拆成两条，
        # 反查时两半都匹配不上库里拼好的整条。
        if pending_header is not None and mods and mods[-1].section == index and _same_affix(
            mods[-1], header
        ):
            mods[-1].lines.append(normalized)
            mods[-1].values.extend(values)
            mods[-1].ranges.extend(ranges)
            mods[-1].raw += "\n" + line
            continue
        mods.append(mod)
        if pending_header is not None:
            # 头只对紧随其后的行组生效，但要留着判断下一行是否同属一条词缀
            pending_header = dict(header)
            pending_header["_consumed"] = True
    return mods


def _same_affix(prev: ParsedMod, header: dict[str, object]) -> bool:
    """当前行是否与上一条属于同一个进阶复制词缀头。"""
    if not header.get("_consumed"):
        return False
    return (
        prev.affix_name == str(header.get("affix_name", ""))
        and prev.tier == header.get("tier")
        and prev.affix_type == str(header.get("affix_type", ""))
    )


def _looks_like_property(m: re.Match[str], language: str) -> bool:
    """`键: 值` 是属性行还是恰好带冒号的词缀？

    只有键在已知表里、或者值是纯数值形态时才算属性。词缀文本里也会出现冒号
    （中文词缀有「效果：」这类写法），认错会把词缀丢进属性表里静默消失。
    """
    key = m.group(1).strip()
    if _norm_key(key, language):
        return True
    value = m.group(2).strip()
    return bool(value) and bool(re.fullmatch(r"[+\-]?[\d.,%\s()a-zA-Z\u4e00-\u9fff-]{0,40}", value)) \
        and bool(_NUM_RE.search(value)) and len(key) <= 20 and not _NUM_RE.search(key)


def _apply_property(item: ParsedItem, key: str, value: str) -> None:
    norm = _norm_key(key, item.language)
    if norm is None:
        item.properties[key] = value
        item.warnings.append(f"未识别的属性行 {key!r}，已原样保留但未参与分析")
        return
    item.properties[norm] = value
    if norm == "item_level":
        item.item_level = _to_int(value)
    elif norm == "quality":
        item.quality = _to_int(value)
    elif norm == "sockets":
        item.sockets = _parse_sockets(value)
    elif norm == "req_level":
        item.requirements.level = _to_int(value)
    elif norm == "req_str":
        item.requirements.strength = _to_int(value)
    elif norm == "req_dex":
        item.requirements.dexterity = _to_int(value)
    elif norm == "req_int":
        item.requirements.intelligence = _to_int(value)
    elif norm == "item_class" and not item.item_class:
        item.item_class = value
    elif norm == "rarity" and item.rarity is Rarity.UNKNOWN:
        item.rarity = _EN_RARITY.get(value.lower()) or _ZH_RARITY.get(value) or Rarity.UNKNOWN


def _assign_mod_kinds(item: ParsedItem, mod_sections: list[tuple[int, list[ParsedMod]]]) -> None:
    """把词缀段落分配到 附魔 / 隐式 / 显式。

    有旗标的直接按旗标走（现代客户端的普通复制都带 `(implicit)`、`(enchant)`）。
    没有旗标的段落按位置推：游戏里隐式段在显式段之前。

    **只在能确定时推断**：出现三段以上无旗标词缀时不硬猜，多出来的进
    `unclassified` 并记告警。猜错会把隐式当显式送去查 T 级，得到一个看似合理
    的错误 T 级——那比说不知道更糟。
    """
    untagged: list[tuple[int, list[ParsedMod]]] = []
    for index, mods in mod_sections:
        tagged = [m for m in mods if m.flags or m.tier is not None or m.affix_type]
        if tagged and len(tagged) == len(mods):
            for mod in mods:
                _place(item, mod)
            continue
        if tagged:
            # 段内混合：有旗标的按旗标放，无旗标的跟随本段多数
            for mod in mods:
                _place(item, mod)
            continue
        untagged.append((index, mods))

    if len(untagged) == 1:
        for mod in untagged[0][1]:
            _place(item, mod)
    elif len(untagged) == 2:
        for mod in untagged[0][1]:
            mod.kind = ModKind.IMPLICIT
            _place(item, mod)
        for mod in untagged[1][1]:
            _place(item, mod)
    elif len(untagged) > 2:
        for mod in untagged[0][1]:
            mod.kind = ModKind.IMPLICIT
            _place(item, mod)
        for mod in untagged[1][1]:
            _place(item, mod)
        for _, mods in untagged[2:]:
            for mod in mods:
                item.unclassified.append(mod.raw)
        item.warnings.append(
            f"有 {len(untagged)} 段无标记词缀，超出「隐式段 + 显式段」的常见结构，"
            f"多出的 {sum(len(m) for _, m in untagged[2:])} 行未归类（不参与分析）"
        )


def _place(item: ParsedItem, mod: ParsedMod) -> None:
    if mod.kind is ModKind.ENCHANT:
        item.enchants.append(mod)
    elif mod.kind in (ModKind.IMPLICIT, ModKind.CORRUPTED_IMPLICIT, ModKind.SYNTHESISED):
        item.implicits.append(mod)
    else:
        item.explicits.append(mod)
