"""从 PoB 的 `Mod*.lua` / `Enchantment*.lua` 抽取词缀。

PoB 的词缀条目形如：

    ["Strength1"] = { type = "Suffix", affix = "of the Brute", "+(8-12) to Strength",
      statOrder = { 1203 }, level = 1, group = "Strength",
      weightKey = { "ring", "amulet", ..., "default" },
      weightVal = { 1000,   1000,   ...,   0 },
      modTags = { "attribute" },
      tradeHashes = { [4080418644] = { "+(8-12) to Strength" } } }

相对 POEDB，它多给三样东西：

1. **按底材标签的权重向量**（`weightKey`/`weightVal`）。POEDB 只给一个总权重，
   而制作成功率必须按具体底材算——项目硬规则禁止「无权重数据支撑的成功率」。
2. **`tradeHashes`**：词缀 → 交易站 stat id，直接打通 Trade API 与 poe.watch。
3. **权威英文文本与词缀名**，可回填给库里 9 万多条只有中文的词缀。

`group` 两边都是 GGG 内部标识符，**字面完全一致**（实测 `IncreasedLife`），
这是跨语言对账的主键。模板不能当对账键：中英文本归一后是两个不同字符串。

归一化复用 POEDB 侧的 `templatize` / `loose_templatize` / `parse_ranges`，
两边同一套规则才谈得上比对。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from kb.sources.pob_lua import LuaOpaque, load_lua_file, parse_return_table
from kb.sources.poedb_mods import loose_templatize, parse_ranges, templatize

logger = logging.getLogger(__name__)


#: 文件 → (词缀种类, 结构形态)。显式声明而不是通配 `Mod*.lua`，因为种类要写进
#: 库里做过滤，猜不出来。未列出的文件会被 `unlisted_mod_files()` 报出来，
#: 这样 PoB 将来新增表不会被静默漏掉。
#:
#: 形态 `flat`   ：`return { [键] = {词缀表} }`，绝大多数
#: 形态 `nested` ：`return { [技能名] = { [难度] = {文本行...} } }`，仅头盔附魔
MOD_FILES: dict[str, tuple[str, str]] = {
    "Data/ModExplicit.lua": ("explicit", "flat"),
    "Data/ModCorrupted.lua": ("corrupted", "flat"),
    "Data/ModEldritch.lua": ("eldritch", "flat"),
    "Data/ModSynthesis.lua": ("synthesis", "flat"),
    "Data/ModItemExclusive.lua": ("item_exclusive", "flat"),
    "Data/ModMaster.lua": ("master", "flat"),
    "Data/ModVeiled.lua": ("veiled", "flat"),
    "Data/ModDelve.lua": ("delve", "flat"),
    "Data/ModJewel.lua": ("jewel", "flat"),
    "Data/ModJewelAbyss.lua": ("jewel_abyss", "flat"),
    "Data/ModJewelCluster.lua": ("jewel_cluster", "flat"),
    "Data/ModJewelCharm.lua": ("jewel_charm", "flat"),
    "Data/ModFlask.lua": ("flask", "flat"),
    "Data/ModTincture.lua": ("tincture", "flat"),
    "Data/ModScourge.lua": ("scourge", "flat"),
    "Data/ModNecropolis.lua": ("necropolis", "flat"),
    "Data/ModGraft.lua": ("graft", "flat"),
    "Data/ModFoulborn.lua": ("foulborn", "flat"),
    "Data/ModFoulbornMap.lua": ("foulborn_map", "flat"),
    "Data/EnchantmentBelt.lua": ("enchant_belt", "flat"),
    "Data/EnchantmentBody.lua": ("enchant_body", "flat"),
    "Data/EnchantmentBoots.lua": ("enchant_boots", "flat"),
    "Data/EnchantmentFlask.lua": ("enchant_flask", "flat"),
    "Data/EnchantmentGloves.lua": ("enchant_gloves", "flat"),
    # 头盔附魔是「技能 × 实验室难度」两层：274 个技能 × MERCILESS/ENDGAME。
    # 按 flat 解析会一条都取不到（顶层值是嵌套表而非词缀表）。
    "Data/EnchantmentHelmet.lua": ("enchant_helmet", "nested"),
    "Data/EnchantmentWeapon.lua": ("enchant_weapon", "flat"),
}

#: 明确排除，并写明理由——免得后来的人以为是漏了。
EXCLUDED_FILES: dict[str, str] = {
    "Data/ModCache.lua": "PoB 运行时缓存，是上面各表的派生物，不是数据源",
    "Data/ModScalability.lua": "词缀数值随等级缩放的系数表，没有可读词缀文本",
    "Data/QueryMods.lua": "交易站查询用的派生索引",
    "Data/ModMap.lua": (
        "PoB **手写**的地图词缀 UI 配置（文件头自述 made by hand），"
        "带 apply=function 闭包与 %d%% printf 占位，无权重、无区间、无 group。"
        "地图词缀应走 POEDB 的中文页面，不从 PoB 取"
    ),
}

#: 已知字段。出现未知字段时告警而不是丢弃——静默丢弃会让
#: 「PoB 没这个信息」和「我们没解析」无法区分。
#:
#: 注意 `tags` 与 `modTags` 是**两回事**，不能合并：
#:   modTags = 分类标签（attribute / life / resistance），给人看、给检索用
#:   tags    = 机制标记（has_attack_mod），决定 weightMultiplier 条件是否生效
#: 合在一起会让「这条是攻击词缀」污染分类标签，检索侧按标签过滤就会出错。
KNOWN_FIELDS = frozenset({
    "type", "affix", "level", "group", "statOrder", "statOrderKey",
    "weightKey", "weightVal", "weightMultiplierKey", "weightMultiplierVal",
    "modTags", "tags", "types", "tradeHashes", "implicitTags",
    "generationWeights", "dropChance", "tier", "influenceTypes",
    "fossilTags", "notFossilTags", "essenceOnly", "affixType", "sd",
    "statOrders", "requiredLevel",
})


@dataclass
class PoBMod:
    """一条 PoB 词缀。字段名贴近 poe-agent 侧的语义，不照抄 Lua 键名。"""

    mod_id: str
    mod_kind: str
    source_file: str
    affix_type: str = ""          # Prefix / Suffix / 空（隐式、附魔等无前后缀之分）
    affix_name: str = ""          # "Hale"、"of the Brute"
    mod_group: str = ""           # 与 POEDB correct_group 同为 GGG 内部 ID
    ilvl: int | None = None       # PoB 的 level = 该档位要求的物品等级
    texts: list[str] = field(default_factory=list)
    weights: dict[str, int] = field(default_factory=dict)
    weight_multipliers: dict[str, int] = field(default_factory=dict)
    #: PoB `modTags`：分类标签（attribute / life / resistance），给检索与展示用
    tags: list[str] = field(default_factory=list)
    #: PoB `tags`：机制标记（has_attack_mod），决定权重乘数条件是否生效。
    #: 与上面的分类标签语义不同，必须分开存。
    behaviour_tags: list[str] = field(default_factory=list)
    #: PoB `types`：适用的物品类型（工艺台词缀专有），如 Body Armour / Ring
    item_types: list[str] = field(default_factory=list)
    #: 来源实例/档位。头盔附魔用它存实验室难度（MERCILESS / ENDGAME）。
    #: **不要塞进 mod_group**：mod_group 是 GGG 的互斥组标识符，是跨语言对账的
    #: 主键，混入难度会让对账按错的键去 join。
    variant_id: str = ""
    trade_hashes: list[int] = field(default_factory=list)
    stat_order: list[int] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)

    # ── 派生 ──

    @property
    def text(self) -> str:
        """多行词缀合并成一行，与 POEDB 的单行文本对齐。"""
        return " / ".join(self.texts)

    @property
    def canonical_text(self) -> str:
        """把 PoB 的负区间记法改写成 POEDB 记法，只用于模板与区间解析。

        `text_en` 保留原文（数值原样保留是硬要求），这里另开一条归一路径。
        """
        return canonicalize_ranges(self.text)

    @property
    def text_template(self) -> str:
        return templatize(self.canonical_text)

    @property
    def loose_template(self) -> str:
        return loose_templatize(self.canonical_text)

    @property
    def ranges(self) -> list[tuple[float, float]]:
        return parse_ranges(self.canonical_text)

    @property
    def range_anomalies(self) -> list[tuple[float, float]]:
        """下界大于上界的区间。正常数据不该出现，出现即说明符号或记法没归一。"""
        return [(a, b) for a, b in self.ranges if a > b]

    @property
    def uid(self) -> str:
        return f"{self.mod_kind}:{self.mod_id}"

    @property
    def spawnable_tags(self) -> list[str]:
        """权重大于 0 的底材标签 —— 即这条词缀实际能出现在哪些底材上。

        权重为 0 的条目是**显式禁止**（如 `IncreasedLife0` 在 `weapon` 上是 0），
        与「没列出」语义不同，所以必须按值筛而不是取全部键。
        """
        return [k for k, v in self.weights.items() if v > 0]

    @property
    def is_consistent(self) -> bool:
        """严格模板的 `#` 个数应恒等于区间数。不等说明解析或上游数据有问题。"""
        return self.text_template.count("#") == len(self.ranges)


# ── 抽取 ───────────────────────────────────────────────────

#: PoB 把负区间写成 `-(15-11)`（负号在括号外），POEDB 写成 `(-15--11)`。
#: 同一个值两种记法，不归一就会把 -15~-11 解析成 15~11：符号丢了，
#: 而且下界大于上界。下游拿它算减伤会算成加伤，是会直接答错的那类缺陷。
_NEG_RANGE = re.compile(r"-\(\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*\)")


def canonicalize_ranges(text: str) -> str:
    """`-(15-11)` → `(-15--11)`，改写成 POEDB 记法以复用同一套归一化函数。

    只改负区间记法，其余原样。这样两个源最终走的是同一条模板/区间解析路径，
    比对才有意义。
    """
    return _NEG_RANGE.sub(lambda m: f"(-{m.group(1)}--{m.group(2)})", text)


def _seq(value) -> list:
    """Lua 的数组表（整数键 1..n）→ Python 列表。非表则返回空。"""
    if not isinstance(value, dict):
        return []
    keys = [k for k in value if isinstance(k, int)]
    return [value[k] for k in sorted(keys)]


def _scalar(value) -> str:
    """取标量字符串。`LuaOpaque` 视为缺失——保留原文没有意义，但要能被发现。"""
    if isinstance(value, LuaOpaque) or value is None:
        return ""
    return str(value)


def parse_mod_entry(mod_id: str, raw: dict, mod_kind: str, source_file: str) -> PoBMod | None:
    """把一条 Lua 表转成 `PoBMod`。结构不符预期时返回 None 并告警。

    不做「尽力凑一条」的兜底：需求 2.7 要求关键字段缺失即视为抽取失败，
    不得以部分字段写入知识库。
    """
    if not isinstance(raw, dict):
        logger.warning("%s: %s 不是表，跳过", source_file, mod_id)
        return None

    texts = [t for t in _seq(raw) if isinstance(t, str) and t.strip()]
    if not texts:
        # 有些条目只有 affix 名没有文本行（如占位组），对知识库无用
        return None

    keys = _seq(raw.get("weightKey"))
    vals = _seq(raw.get("weightVal"))
    if len(keys) != len(vals):
        logger.warning(
            "%s: %s 的 weightKey(%d) 与 weightVal(%d) 长度不等，权重按短的截断",
            source_file, mod_id, len(keys), len(vals),
        )
    weights = {
        str(k): int(v)
        for k, v in zip(keys, vals)
        if isinstance(v, (int, float))
    }

    mkeys = _seq(raw.get("weightMultiplierKey"))
    mvals = _seq(raw.get("weightMultiplierVal"))
    multipliers = {
        str(k): int(v)
        for k, v in zip(mkeys, mvals)
        if isinstance(v, (int, float))
    }

    trade = raw.get("tradeHashes")
    trade_hashes = (
        sorted(int(k) for k in trade if isinstance(k, int))
        if isinstance(trade, dict) else []
    )

    level = raw.get("level")
    unknown = sorted(
        str(k) for k in raw
        if isinstance(k, str) and k not in KNOWN_FIELDS
    )
    # `types` 是 { ["Body Armour"] = true } 形式的集合表，不是数组
    types_raw = raw.get("types")
    item_types = (
        sorted(str(k) for k, v in types_raw.items() if isinstance(k, str) and v)
        if isinstance(types_raw, dict) else []
    )
    group = _scalar(raw.get("group"))
    affix_type = _scalar(raw.get("type"))
    affix_name = _scalar(raw.get("affix"))

    return PoBMod(
        mod_id=stable_mod_id(
            mod_id, group, level, texts, affix_name, affix_type, item_types
        ),
        mod_kind=mod_kind,
        source_file=source_file,
        affix_type=affix_type,
        affix_name=affix_name,
        mod_group=group,
        ilvl=int(level) if isinstance(level, (int, float)) else None,
        texts=texts,
        weights=weights,
        weight_multipliers=multipliers,
        tags=[str(t) for t in _seq(raw.get("modTags")) if isinstance(t, str)],
        behaviour_tags=[str(t) for t in _seq(raw.get("tags")) if isinstance(t, str)],
        item_types=item_types,
        trade_hashes=trade_hashes,
        stat_order=[int(s) for s in _seq(raw.get("statOrder")) if isinstance(s, (int, float))],
        unknown_fields=unknown,
    )


def stable_mod_id(
    lua_key: str,
    group: str,
    level,
    texts: list[str],
    affix: str,
    affix_type: str = "",
    item_types: list[str] | None = None,
) -> str:
    """给 uid 一个稳定标识。

    多数文件用字符串键（`IncreasedLife7`），直接用即可。但 `ModMaster.lua` 用的是
    **位置项**（`{ type = "Prefix", ... }` 没有键名），Lua 语义下键就是 1..n。
    拿数字当 uid 会在 PoB 增删任一条目时让后面全部错位，历史数据全部对不上。
    因此对数字键改用内容哈希——内容不变则 id 不变。

    `item_types` 必须进哈希（实测踩到）：`ModMaster.lua` 里有两条工艺台词缀
    文本、组、等级完全相同，**只差适用物品类型**——一条给戒指/护身符/手套，
    一条给箭袋。不含 types 就会撞 uid，箭袋那条被 UPSERT 吞掉，
    「工艺台能给箭袋加这条吗」就会答错。
    """
    if not lua_key.isdigit():
        return lua_key
    seed = "|".join(
        [group, str(level), affix_type, affix, *sorted(item_types or []), *texts]
    )
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return f"{group or 'nogroup'}~{digest}"


def parse_nested_enchant(
    outer_key: str, raw: dict, mod_kind: str, source_file: str
) -> list[PoBMod]:
    """头盔附魔：`[技能名] = { [难度] = { 文本行... } }`。

    一个 (技能, 难度) 组合的多行文本是**一次附魔同时给的全部效果**，
    因此合成一条记录，而不是拆成多条独立词缀——拆开会让「这个附魔给什么」
    的答案漏掉其余几行。
    """
    out: list[PoBMod] = []
    if not isinstance(raw, dict):
        return out
    for difficulty, lines in raw.items():
        texts = [t for t in _seq(lines) if isinstance(t, str) and t.strip()]
        if not texts:
            continue
        out.append(
            PoBMod(
                mod_id=f"{outer_key}|{difficulty}",
                mod_kind=mod_kind,
                source_file=source_file,
                affix_name=str(outer_key),
                # 难度即实验室层级（MERCILESS / ENDGAME），决定数值档位。
                # 放 variant_id 而非 mod_group：后者是对账主键，不能被污染。
                variant_id=str(difficulty),
                texts=texts,
            )
        )
    return out


def extract_file(
    path: Path, mod_kind: str, source_file: str, form: str = "flat"
) -> list[PoBMod]:
    """抽取单个文件。解析异常向上抛——不静默跳过整个文件。"""
    table = parse_return_table(load_lua_file(path))
    out: list[PoBMod] = []
    for mod_id, raw in table.items():
        if form == "nested":
            out.extend(parse_nested_enchant(str(mod_id), raw, mod_kind, source_file))
            continue
        mod = parse_mod_entry(str(mod_id), raw, mod_kind, source_file)
        if mod is not None:
            out.append(mod)
    return out


def extract_all(pob_root: Path) -> tuple[list[PoBMod], list[str]]:
    """抽取全部已声明的词缀文件。返回 (词缀列表, 问题列表)。"""
    mods: list[PoBMod] = []
    problems: list[str] = []
    for rel, (kind, form) in MOD_FILES.items():
        path = pob_root / "src" / rel
        if not path.is_file():
            problems.append(f"{rel}：文件不存在（PoB 版本可能已移除该表）")
            continue
        try:
            got = extract_file(path, kind, rel, form)
        except Exception as exc:  # noqa: BLE001  上游格式变化要看得见
            problems.append(f"{rel}：解析失败 {type(exc).__name__}: {exc}")
            continue
        if not got:
            problems.append(
                f"{rel}：按 {form} 形态解析出 0 条，形态声明可能已过时"
            )
        mods.extend(got)

    for rel, why in unlisted_mod_files(pob_root).items():
        problems.append(f"{rel}：未登记的词缀文件（{why}），需确认是否要纳入")

    unknown: dict[str, set[str]] = {}
    for m in mods:
        for f in m.unknown_fields:
            unknown.setdefault(f, set()).add(m.source_file)
    for f, files in sorted(unknown.items()):
        problems.append(f"未知字段 `{f}`：出现于 {', '.join(sorted(files))}（当前被忽略）")

    return mods, problems


def unlisted_mod_files(pob_root: Path) -> dict[str, str]:
    """找出既没登记也没明确排除的 `Mod*.lua` / `Enchantment*.lua`。"""
    out: dict[str, str] = {}
    data = pob_root / "src" / "Data"
    if not data.is_dir():
        return out
    for path in sorted(data.glob("Mod*.lua")) + sorted(data.glob("Enchantment*.lua")):
        rel = f"Data/{path.name}"
        if rel in MOD_FILES or rel in EXCLUDED_FILES:
            continue
        out[rel] = f"{path.stat().st_size // 1024} KB"
    return out
