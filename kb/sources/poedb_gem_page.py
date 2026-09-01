"""POEDB 技能石**详情页**抽取器：每级属性需求 + 每级效果文本。

## 为什么要抓详情页（第三十二轮）

从第十二轮起，宝石有两维一直写在 `partial_note` 的「未采集」里：

* **效果描述文本**——PoB 侧只有 stat id 与乘数（`spell_minimum_base_cold_damage`、
  `0.8`），要按 `Modules/StatDescriber.lua` 那 292 行才渲染成一句话，而且渲染出来
  **是英文**。「多重投射(辅)给什么加成」这种最常见的问题因此只能拒答。
* **属性需求（力/敏/智）**——`Gems.lua` 的 `reqStr/reqDex/reqInt` 是换算系数不是显示值
  （真需求要按 `round((20+(等级需求-3)×3)×(系数/100)^0.9×statType)` 算）。

第三十一轮为了取中文名抓了 11 张详情页，顺手看见了这一页真正的价值：
**每颗宝石都有一张逐级表**，列是「等级 ｜ 需求等级 ｜ 力量/敏捷/智慧 ｜ 魔力 ｜
基本伤害 ｜ <每条效果文本> ｜ 经验值」，行是 1..40 级。也就是说上面两维
**中文的、按等级的**都在这一页上，不需要自己渲染，也不需要自己算。

## 这一页自带对账口径

那张表里 `需求等级` 与 `魔力` 两列，本库早就有 PoB 侧的值（`pob_skill_level`）。
所以每一颗宝石、每一级都能逐格比对——**对账不是抽样而是全量**，而且是在入库前做：
对不上就不入这颗宝石，并把差异登记成跨来源分歧。

实测确实抓到了真分歧：`Convocation` PoB 侧发的是重做后的 `ConvocationNew`
（各级需求等级恒为 31），而 POEDB 的 `/cn/Convocation` 仍是旧版（24/27/30…）。
两边讲的不是同一颗宝石，那就**不能把旧版的效果文本挂到新版身上**。

## 结构列是白名单，其余列一律当效果文本

结构列只有八个词：`等级`、`需求等级`、`力量`、`敏捷`、`智慧`、`魔力`、
`基本伤害`、`经验值`（2026-08-20 实测 88 张缓存页全覆盖）。判据写成白名单而不是
「短的就是结构列」：哪天上游多一列 `生命`（血魔法消耗），白名单会**报错**，
而「短的就是结构列」会把它静默当成一条效果文本、或反过来把某条效果文本当成消耗列。

`基本伤害` 列刻意不取：它是三个伤害效果百分比（`100%, 100%, 100%`），
而真正的基础伤害第二十九轮已经按 PoB 的 effectiveness 公式算好入库、并与本页对过账。
`经验值` 也不取——那是升级所需经验，本库没有任何出口要用它，
入库了就是又一份没有出口的数据（第二十九轮的教训）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

#: 逐级表的定位判据：前两列必须正好是这两个词。按表头定位而不是按下标取表——
#: 详情页上还有「品质变体」「任务奖励」等好几张表，下标会随上游加表而错位。
LEVEL_TABLE_HEAD = ("等级", "需求等级")

#: 属性需求列。
ATTR_COLUMNS = {"力量": "req_str", "敏捷": "req_dex", "智慧": "req_int"}
#: 消耗列 → PoB `cost` 里的资源名（对账用；本模块不存消耗，PoB 侧已有）。
COST_COLUMNS = {"魔力": "Mana", "生命": "Life"}
#: 取到了也不入库的结构列，理由见模块 docstring。
IGNORED_COLUMNS = ("基本伤害", "经验值")

_SPACE_RE = re.compile(r"\s+")
#: 单元格里的第一个数（允许千分位与小数），用于属性需求与对账。
_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


class GemPageError(ValueError):
    """详情页结构与预期不符。**定位失败即拒绝**，不猜列。"""


@dataclass(frozen=True)
class GemStatLine:
    """一条效果文本（表上的一列）。

    `text_zh` 是列头原文，里面带着**1 级的数值**（「造成 21 - 31 物理伤害」）——
    这是 POEDB 的写法，保留原样：改写成 `#` 占位就等于自己重写上游文本。
    每一级的实际值在 `GemLevelRow.stat_values` 里。
    """

    index: int
    text_zh: str


@dataclass(frozen=True)
class GemLevelRow:
    gem_level: int
    level_requirement: int | None = None
    req_str: int | None = None
    req_dex: int | None = None
    req_int: int | None = None
    #: 资源名 → 值。**只用于对账**，不入库（PoB 侧已有这一维）。
    costs: dict[str, float] = field(default_factory=dict)
    #: 效果文本列 index → 该级的值（原文，可能是 `4`、`0.7`、`16` 或空）
    stat_values: dict[int, str] = field(default_factory=dict)

    @property
    def requirements(self) -> dict[str, int]:
        """只含页面真的给了的那几项——补 0 会让「不需要智力」和「没有这一列」同义。"""
        out = {}
        for key, value in (("str", self.req_str), ("dex", self.req_dex), ("int", self.req_int)):
            if value is not None:
                out[key] = value
        return out


@dataclass(frozen=True)
class GemPage:
    poedb_slug: str
    stat_lines: tuple[GemStatLine, ...]
    levels: tuple[GemLevelRow, ...]

    @property
    def max_level(self) -> int:
        return max((lv.gem_level for lv in self.levels), default=0)


def _text(node) -> str:  # noqa: ANN001
    return _SPACE_RE.sub(" ", node.text(deep=True)).strip() if node is not None else ""


def _number(cell: str) -> float | None:
    matched = _NUM_RE.search(cell)
    if matched is None:
        return None
    return float(matched.group(0).replace(",", ""))


def _int_or_none(cell: str) -> int | None:
    value = _number(cell)
    if value is None or value != int(value):
        return None
    return int(value)


#: 「看起来像结构列」的判据：四个字以内、且**没有数值或 `#` 占位**。
#: 效果文本列头总带着 1 级的数值或占位符（「造成 21 - 31 物理伤害」、「范围 # 米」）——
#: 2026-08-20 实测 462 张缓存页里唯一的短列头就是「范围 # 米」，它有 `#`。
def _looks_structural(header: str) -> bool:
    compact = header.replace(" ", "")
    return len(compact) <= 4 and not re.search(r"[\d#]", compact)


def _classify(headers: list[str]) -> tuple[dict[int, str], tuple[GemStatLine, ...]]:
    """列头 → (结构列位置, 效果文本列)。

    结构列走白名单；剩下的都是效果文本。**空列头直接报错**：那说明表结构变了，
    静默跳过会让某颗宝石少一条效果文本，而少了没人看得出来。
    """
    structural: dict[int, str] = {}
    stat_lines: list[GemStatLine] = []
    for position, header in enumerate(headers):
        if not header:
            raise GemPageError(f"逐级表第 {position + 1} 列没有列头")
        if position == 0 and header == LEVEL_TABLE_HEAD[0]:
            structural[position] = "gem_level"
        elif header == LEVEL_TABLE_HEAD[1]:
            structural[position] = "level_requirement"
        elif header in ATTR_COLUMNS:
            structural[position] = ATTR_COLUMNS[header]
        elif header in COST_COLUMNS:
            structural[position] = f"cost:{COST_COLUMNS[header]}"
        elif header in IGNORED_COLUMNS:
            structural[position] = ""
        elif _looks_structural(header):
            # 白名单之外的**短**列头：那更可能是上游新加的结构列（比如血魔法技能的
            # `生命` 消耗），而不是一条效果文本。报错而不是当成效果文本——
            # 猜错的两个方向都会静默出错：当成效果文本会把消耗写进「效果」，
            # 当成消耗列会让一条效果文本凭空消失。
            raise GemPageError(
                f"逐级表出现白名单之外的结构列候选 {header!r}，"
                "先确认它是消耗/需求还是效果文本，再决定放进哪一份白名单"
            )
        else:
            stat_lines.append(GemStatLine(index=len(stat_lines), text_zh=header))
    if "gem_level" not in structural.values():
        raise GemPageError("逐级表没有等级列")
    return structural, tuple(stat_lines)


def parse_gem_page(html: str, *, poedb_slug: str) -> GemPage | None:
    """解析一张宝石详情页的逐级表。页面上没有这张表就返回 None。

    返回 None 而不是抛异常：实测有宝石的详情页确实不带逐级表（页面只有说明），
    那是**数据缺口**而不是结构错误，由调用方登记进台账。
    """
    tree = HTMLParser(html)
    for table in tree.css("table"):
        headers = [_text(th) for th in table.css("th")]
        if tuple(headers[:2]) != LEVEL_TABLE_HEAD:
            continue
        structural, stat_lines = _classify(headers)
        levels: list[GemLevelRow] = []
        for row in table.css("tr"):
            cells = [_text(td) for td in row.css("td")]
            if len(cells) < 2:
                continue
            fields: dict[str, object] = {}
            costs: dict[str, float] = {}
            stat_values: dict[int, str] = {}
            stat_position = 0
            for position, cell in enumerate(cells):
                role = structural.get(position)
                if role is None:  # 效果文本列
                    if cell:
                        stat_values[stat_position] = cell
                    stat_position += 1
                    continue
                if not role:  # 白名单里但刻意不取
                    continue
                if role.startswith("cost:"):
                    value = _number(cell)
                    if value is not None:
                        costs[role.split(":", 1)[1]] = value
                else:
                    fields[role] = _int_or_none(cell)
            level = fields.pop("gem_level", None)
            if not isinstance(level, int):
                continue  # 表尾的合计行之类：没有等级就不是一级
            levels.append(
                GemLevelRow(
                    gem_level=level,
                    level_requirement=fields.get("level_requirement"),  # type: ignore[arg-type]
                    req_str=fields.get("req_str"),  # type: ignore[arg-type]
                    req_dex=fields.get("req_dex"),  # type: ignore[arg-type]
                    req_int=fields.get("req_int"),  # type: ignore[arg-type]
                    costs=costs,
                    stat_values=stat_values,
                )
            )
        if not levels:
            raise GemPageError(f"{poedb_slug} 的逐级表定位到了却没有任何等级行")
        return GemPage(
            poedb_slug=poedb_slug, stat_lines=stat_lines, levels=tuple(levels)
        )
    return None


#: 「固定」表的定位判据（第四十四轮）。这张表是**与等级无关**的技能机制
#: （「有 25% 的几率对敌人造成冻结状态」），与 `LEVEL_TABLE_HEAD` 那张逐级表
#: 不重叠——后者是「造成 25 - 38 冰霜伤害」这种随等级变的值。
FIXED_TABLE_HEAD = ("固定",)

#: 固定表里**不是内容**的那些行：`CooldownTime: 5000`、`Mana: 50%`。
#: 2026-09-01 实测 3607 行里有 515 行是这个形状，键只有两个（Mana 287 / CooldownTime 228）。
#: 它们不入库（把内部字段当中文答案输出，就是拿调试字段冒充游戏文本），
#: 但**它们是这一维唯一的对账口径**——两项 PoB 侧都有，见 `reconcile_fixed`。
_FIXED_KV_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)\s*:\s*(\S+)")

#: 上游在文本里写的是**字面的反斜杠加 n**，不是换行符（实测 126 条含它）。
_LITERAL_NEWLINE = "\\n"


@dataclass(frozen=True)
class GemFixedStats:
    """一颗宝石的固定效果，**内容与对账口径在类型上就分开**。

    分开是为了让下游不可能不小心把 `CooldownTime: 5000` 渲染给用户——
    那是这个项目在怪物词缀那一轮定下的禁忌（63 条只有未翻译 stat id 的行
    宁可不入库，也不拿调试字段冒充游戏文本）。
    """

    #: 中文效果文本，按页面顺序。
    prose: tuple[str, ...] = ()
    #: `Mana` / `CooldownTime` 这类内部字段，**只用于对账**，不入库。
    reconcile_kv: dict[str, str] = field(default_factory=dict)


def _in_active_tab(node) -> bool | None:  # noqa: ANN001
    """这个节点在不在**当前选中的**标签页里。不在任何标签页里返回 None。"""
    parent = node.parent
    while parent is not None:
        classes = parent.attributes.get("class") or ""
        if "tab-pane" in classes:
            return "active" in classes
        parent = parent.parent
    return None


def parse_gem_fixed(html: str, *, poedb_slug: str) -> GemFixedStats:
    """解析「固定」表。页面上没有这张表就返回空的 `GemFixedStats`。

    返回空而不是抛异常：实测 21 颗（幻化守卫、绝望、元素要害…）页面上没有这张表，
    那是**这个技能没有与等级无关的固定机制**，既不是结构错误、也不是采集缺口。

    ## 为什么不能「取第一张表」

    实测 820 张页里 **280 张有两张「固定」表**，而其中 **98 张两张内容不一样**
    （静滞之天雷之珠 `基础范围为 2.8 米` vs `1.8 米`；巨镰之飞刃风暴
    `基础持续时间为 0.8 秒` vs `1.5 秒`）。取第一张就是在两个互相矛盾的说法里
    **无依据地挑一个**——这正是第一原则要拦的事。

    页面自己给了判据：两张表在**不同的标签页**里，而选中的那个标签标着
    **这颗宝石的中文名**（`巨镰之飞刃风暴 SkillGemBladeVortex`），
    另一个是次要视图。所以取**选中标签页**里的那张。

    没有选中态时的退路很窄：只有**恰好一张**表时才用它（没有可挑的，就没有挑错的
    风险，实测 5 颗如此）；多张且都没选中就抛错——那时确实无从判断。

    两个实测出来的细节：

    * **用 `_text()` 而不是 `text(strip=True)`**：数字在 `<span class="mod-value">`
      里，`strip=True` 会把两边的空格吃掉得到 `增助攻击的暴击率+25%`，
      而页面上是 `增助攻击的暴击率 +25%`。
    * **字面 `\\n` 换成一个空格，不拆行**：它有时夹在句子中间（奋发之赦罪的
      「对召唤生物伤害的增幅与减益\\n有 250% 也作用于该技能的伤害」），
      拆开会得到两个都读不通的碎片。一个单元格是上游的一个断言单位。
    """
    tree = HTMLParser(html)
    tables = [
        table for table in tree.css("table")
        if tuple(_text(th) for th in table.css("thead th")) == FIXED_TABLE_HEAD
    ]
    if not tables:
        return GemFixedStats()
    active = [t for t in tables if _in_active_tab(t) is True]
    if active:
        chosen = active[0]
    elif len(tables) == 1:
        chosen = tables[0]
    else:
        raise GemPageError(
            f"{poedb_slug} 有 {len(tables)} 张固定表且都不在选中的标签页里，"
            "无法判断哪一张描述的是这颗宝石"
        )
    prose: list[str] = []
    kv: dict[str, str] = {}
    for row in chosen.css("tbody tr"):
        cells = row.css("td")
        if len(cells) != 1:
            raise GemPageError(
                f"{poedb_slug} 的固定表某行有 {len(cells)} 格，应为 1 格"
            )
        text = _text(cells[0])
        if not text:
            continue
        matched = _FIXED_KV_RE.match(text)
        if matched:
            kv[matched.group(1)] = matched.group(2)
            continue
        prose.append(text.replace(_LITERAL_NEWLINE, " ").strip())
    return GemFixedStats(prose=tuple(prose), reconcile_kv=kv)


#: 「使命」表的定位判据（第四十三轮）。同样按表头定位而不是按下标——
#: 这一页上「逐级表」「品质变体」「使命」好几张表并存。
QUEST_TABLE_HEAD = ("章节", "使命", "NPC", "角色")

#: NPC 列图标的 `title` → 归一后的获取途径。
#: 三个取值都是 2026-09-01 在 820 张缓存页上实测出来的，不是猜的分类。
QUEST_SOURCE_KINDS = {
    "任务奖励": "quest_reward",
    "任务商人": "vendor",
    "Box": "box",
}

#: POE1 的七个职业。`全职业` 要展开成它，否则「女巫能不能拿」这类问题
#: 在 `全职业` 的行上会答不出来。
CLASSES = ("野蛮人", "决斗者", "游侠", "暗影", "女巫", "圣堂武僧", "贵族")
ALL_CLASSES_TOKEN = "全职业"

#: 角色列的分隔符（实测只有这一个）。
_ROLE_SEP = "·"


@dataclass(frozen=True)
class GemQuestRow:
    """一条获取途径：某颗宝石在第几章、由哪个使命、以什么方式、给哪些职业。

    **`act` 是这一行的事实，不是「使命」的属性。** 实测 1265 行里有 2 行
    （熔岩护盾 / 黑色入侵者 / 奈莎、斩首（辅）/ 永恒梦魇）的章节与同名使命的
    其余行不同，两条都是**商人行**。所以不做「使命 → 章节」归一——
    那等于替上游改数据。（一开始假设它是 1:1 的是**我的前提**，页面从没这么说过。）
    """

    act: int
    quest_zh: str
    quest_slug: str
    #: `quest_reward` / `vendor` / `box`
    source_kind: str
    #: 商人名；任务奖励与起始箱没有 NPC，为 None。
    npc_zh: str | None
    #: 展开后的职业（`全职业` → 七个），去重且保持页面顺序。
    classes: tuple[str, ...]
    #: 角色列原文（`全职业` 原样留着——用户问「全职业能拿吗」时要能引用到这个词）。
    classes_raw: str


def _quest_classes(cell: str) -> tuple[tuple[str, ...], str]:
    """角色列 → (展开去重后的职业, 原文)。

    两件实测出来的事：`全职业` 要展开成七个职业；同一格可能把一个职业写两遍
    （「能量之刃」是 `暗影·暗影`），所以要去重——不去重会让「哪些职业能拿」
    数出重复项。
    """
    raw = _SPACE_RE.sub(" ", cell).strip()
    if not raw:
        return (), ""
    if ALL_CLASSES_TOKEN in raw:
        return CLASSES, raw
    seen: list[str] = []
    for part in raw.split(_ROLE_SEP):
        name = part.strip()
        if name and name not in seen:
            seen.append(name)
    return tuple(seen), raw


def parse_gem_quests(html: str, *, poedb_slug: str) -> tuple[GemQuestRow, ...]:
    """解析「使命」表。页面上没有这张表就返回空元组。

    返回空而不是抛异常：实测 820 颗里 **152 颗**（瓦尔 52、觉醒辅助、契约类）
    页面上根本没有这张表——它们本来就不由任务给，是**数据缺口而非结构错误**，
    由调用方登记进台账。

    `source_kind` 取自 NPC 列图标的 `title` 而不是单元格文本，理由是实测的两个形态：

    * **起始箱那 21 行 NPC 单元格是空的**（章节 1「绝望岩滩」，七个职业的开局技能
      火球 / 重击 / 灵体投掷…）。按文本判会把这 21 行静默丢掉。
    * **4 行上游漏了图标**，但单元格文本仍是「任务奖励」——所以没有图标时回退取文本。
    """
    tree = HTMLParser(html)
    for table in tree.css("table"):
        headers = [_text(th) for th in table.css("thead th")]
        if tuple(headers) != QUEST_TABLE_HEAD:
            continue
        rows: list[GemQuestRow] = []
        for row in table.css("tbody tr"):
            cells = row.css("td")
            if len(cells) != len(QUEST_TABLE_HEAD):
                raise GemPageError(
                    f"{poedb_slug} 的使命表某行有 {len(cells)} 格，"
                    f"应为 {len(QUEST_TABLE_HEAD)} 格"
                )
            act = _int_or_none(_text(cells[0]))
            if act is None:
                raise GemPageError(f"{poedb_slug} 的使命表某行章节列不是数字")
            quest_link = cells[1].css_first("a")
            quest_zh = _text(cells[1])
            quest_slug = (
                (quest_link.attributes.get("href") or "").rsplit("/", 1)[-1]
                if quest_link is not None
                else ""
            )
            icon = cells[2].css_first("i")
            title = (icon.attributes.get("title") or "") if icon is not None else ""
            # 没有图标时回退到单元格文本（实测 4 行）。
            marker = title or _text(cells[2])
            kind = QUEST_SOURCE_KINDS.get(marker)
            if kind is None:
                raise GemPageError(
                    f"{poedb_slug} 的使命表出现未知的获取途径标记 {marker!r}，"
                    "先确认它是任务奖励、商人还是箱子，再决定怎么归一"
                )
            npc_link = cells[2].css_first("a")
            classes, classes_raw = _quest_classes(_text(cells[3]))
            rows.append(
                GemQuestRow(
                    act=act,
                    quest_zh=quest_zh,
                    quest_slug=quest_slug,
                    source_kind=kind,
                    npc_zh=_text(npc_link) or None if npc_link is not None else None,
                    classes=classes,
                    classes_raw=classes_raw,
                )
            )
        if not rows:
            raise GemPageError(f"{poedb_slug} 的使命表定位到了却没有任何行")
        return tuple(rows)
    return ()


@dataclass(frozen=True)
class Mismatch:
    """一处跨来源不一致。写清楚是哪一级、哪一项、两边各是什么。"""

    gem_level: int
    field: str
    poedb: str
    pob: str

    def render(self) -> str:
        return f"{self.gem_level} 级 {self.field}：POEDB {self.poedb} / PoB {self.pob}"


#: 伤害列的判据。**必须只匹配到一列**才用它对账：实测 10 颗宝石有两列都像伤害
#: （技能自己的伤害 + 被辅助技能附加的伤害），那时不知道该跟哪一列比，就不比。
_DAMAGE_COLUMN_RE = re.compile(r"(造成|附加).*伤害")
_CELL_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def damage_column(page: GemPage) -> GemStatLine | None:
    """页面上唯一的那条伤害文本列；有零条或多条都返回 None（不猜）。"""
    matched = [line for line in page.stat_lines if _DAMAGE_COLUMN_RE.search(line.text_zh)]
    return matched[0] if len(matched) == 1 else None


def reconcile(page: GemPage, pob_levels: dict[int, dict[str, object]]) -> list[Mismatch]:
    """逐级比对 `需求等级`、消耗与**基础伤害**。两边都有值时才比——缺一侧不算分歧。

    这是**入库前**的门槛：对不上说明两侧讲的不是同一颗宝石（实测 `Convocation`：
    PoB 发的是重做后的 `ConvocationNew`，各级需求等级恒为 31，而 POEDB 那一页还是
    旧版的 24/27/30…），那就不能把这一页的效果文本挂到库里那颗宝石身上。

    伤害那一项顺带把第二十九轮的抽样对账变成了**全量对账**：那一轮用
    `scripts/verify_gem_damage.py` 手抠 20 颗宝石的区间文本，20/20 一致；
    这里是同一份数据的逐级表，41 颗有伤害列的缓存页 41/41 每一级都一致。
    """
    out: list[Mismatch] = []
    damage = damage_column(page)
    for row in page.levels:
        pob = pob_levels.get(row.gem_level)
        if pob is None:
            continue
        expected = pob.get("level_requirement")
        if row.level_requirement is not None and expected is not None:
            if int(row.level_requirement) != int(expected):  # type: ignore[arg-type]
                out.append(Mismatch(row.gem_level, "需求等级",
                                    str(row.level_requirement), str(expected)))
        pob_costs = pob.get("cost") or {}
        for resource, value in row.costs.items():
            other = pob_costs.get(resource)  # type: ignore[union-attr]
            if other is None:
                continue
            if abs(float(value) - float(other)) > 0.01:
                out.append(Mismatch(row.gem_level, f"{resource} 消耗",
                                    f"{value:g}", f"{float(other):g}"))
        base = pob.get("base_damage")
        cell = row.stat_values.get(damage.index) if damage else None
        if base and cell:
            nums = [float(n.replace(",", "")) for n in _CELL_NUM_RE.findall(cell)]
            low, high = base  # type: ignore[misc]
            if len(nums) == 2 and (abs(nums[0] - low) > 1 or abs(nums[1] - high) > 1):
                out.append(Mismatch(row.gem_level, "基础伤害",
                                    f"{nums[0]:g}–{nums[1]:g}", f"{low:g}–{high:g}"))
    return out


def reconcile_fixed(
    fixed: GemFixedStats, pob_level_one: dict[str, object] | None
) -> list[Mismatch]:
    """用固定表里那些 `Key: value` 行跟 PoB 对账（第四十四轮）。

    **这一维本来没有第二个来源**（固定效果只有 POEDB 给中文），所以对账口径只能
    从这一页自己身上找。找到的就是那 515 行不入库的内部字段：

    * `CooldownTime: 5000` 是**毫秒**，PoB 的 `cooldown` 是**秒**——差 1000 倍，
      不换算就会把 8/8 一致报成 8/8 不符。
    * `Mana: 50%` 是**保留百分比**（愤怒/清晰这类光环），不是定值消耗；
      带 `%` 的走保留、不带的走消耗，混起来比会把光环全判成分歧。

    与第三十二轮「需求等级/魔力两列本库早就有，所以对账是全量而不是抽样」同一形状：
    能对的就全对，对不上整颗拒绝。
    """
    out: list[Mismatch] = []
    if not fixed.reconcile_kv or not pob_level_one:
        return out
    raw_cd = fixed.reconcile_kv.get("CooldownTime")
    pob_cd = pob_level_one.get("cooldown")
    if raw_cd is not None and pob_cd is not None:
        value = _number(raw_cd)
        if value is not None and abs(value / 1000.0 - float(pob_cd)) > 0.01:  # type: ignore[arg-type]
            out.append(Mismatch(1, "冷却时间",
                                f"{value / 1000:g} 秒", f"{float(pob_cd):g} 秒"))  # type: ignore[arg-type]
    raw_mana = fixed.reconcile_kv.get("Mana")
    if raw_mana is not None:
        value = _number(raw_mana)
        if value is None:
            return out
        if raw_mana.endswith("%"):
            expected = pob_level_one.get("reservation_percent")
            label = "魔力保留%"
        else:
            expected = (pob_level_one.get("cost") or {}).get("Mana")  # type: ignore[union-attr]
            label = "魔力消耗"
        if expected is not None and abs(value - float(expected)) > 0.01:  # type: ignore[arg-type]
            out.append(Mismatch(1, label, f"{value:g}", f"{float(expected):g}"))  # type: ignore[arg-type]
    return out
