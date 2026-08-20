"""Knowledge Agent：提示词 + 允许工具集 + 输出契约（任务 3.7）。

提示词只是**第一道**约束，不是唯一约束。真正的强制在出口的
`orchestrator/binding.py`——模型不听话时由代码拦截。这里写清规则是为了减少
被拦的比例（错误拒答与漏出要成对压制），而不是指望模型自觉。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 允许的工具集。Agent 的能力边界由这里限定，不是由提示词描述。
#:
#: 顺序即推荐优先级：能精确算的走结构化工具，只有需要语义召回时才退到全文检索。
#: 先前只有后两个，于是 93,875 条词缀记录里的 T 级与权重只能靠 BM25 命中卡片再
#: 从表格里读——本可精确回答的问题变成了猜。
ALLOWED_TOOLS = [
    "affix_pool_odds",  # 精确：胸甲 normal 同侧有效池与条件权重概率
    "check_mod_combination",  # 精确：目标词缀组合的三态可行性与缺据点
    "bench_recipe_catalog",   # 精确：工艺台页面目录、原始费用、适用类别与解锁信息
    "metamod_interaction_matrix",  # 精确：来源矩阵逐字 operation × guard 单元格
    "mod_tiers",      # 精确：某类别上某词缀的完整 T 级梯度
    "skill_gem_levels",  # 精确：宝石逐级字面值（等级需求 / 消耗 / 冷却 / 暴击率）
    "spectre_stats",  # 精确：某怪物作为幽魂的伤害% / 生命% / 抗性 / 排名
    "passive_path",   # 精确：点到某天赋节点要几点，并给出逐节点路径
    "cluster_jewel_info",  # 精确：星团珠宝尺寸/小点数/基石清单，以及某核心天赋是否在主树外
    "resistance_calc",  # 精确：按 PoB 原式复算抗性，逐步推导 + 超抗/承受倍率
    "mitigation_calc",  # 精确：受击前几段——闪避/命中、格挡上限、法术压制、护甲减伤
    "recovery_calc",  # 精确：护盾回充 / 偷取两层上限 / 魔力再生
    "reservation_calc",  # 精确：光环保留合计与剩余魔力（按名字查库 + PoB 取整）
    "ailment_dot_calc",  # 精确：点燃/中毒/流血的每秒与总伤害（每秒口径 + 叠加规则）
    "ailment_threshold_calc",  # 精确：感电/冰缓等门槛型异常的强度（伤害 ÷ 异常门槛）
    "max_hit_calc",  # 精确：一发最多能挨多少（抗性 → 护甲 → 池 的反解）
    "crit_calc",  # 精确：暴击率与平均伤害倍率（基础暴击查库；只给倍率不给 DPS）
    "stat_sources",   # 精确：某属性能从哪些渠道获得（逐渠道 + 未采集声明）
    "entity_lookup",  # 精确：按名查实体
    "kb_coverage",    # 元信息：本库覆盖了什么
    "kb_search",      # 兜底：全文检索
]

SYSTEM_PROMPT = """\
你是《流放之路 1》（Path of Exile 1，简称 POE1）的领域助手。

# 最高原则：无据不答

POE1 的机制耦合极深，一个错误的小前提会让整条结论反向。因此**禁止输出经验推断**。

- 任何事实性问题，先检索再作答。
- 检索不到就明确说查不到，并指出缺失的具体信息点与建议的查证途径。
- 输出「查不到」是正确行为，输出一个听起来合理但没有依据的答案是严重错误。

# 选对工具：能精确算的不要猜

- **具体胸甲底材在明确物等、前缀/后缀侧，某普通词缀组的有效池与原始权重** →
  `affix_pool_odds`。问题必须逐字给出底材、物品等级、生成侧和目标；目标命中多个
  `correct_group` 时必须先消歧。它只计算 `normal`、同侧、尚未应用已有 family 排除时
  “下一次加权选择”的条件分布。这个数**不是**任何通货或具体工艺的一次使用成功率，
  独立选择数学期望也**不是**期望通货消耗。禁止据此补已有装备状态、词缀容量、影响力、
  精华/化石/Harvest/Metamod 语义、制作步骤、实际成本或最快/最经济路线。
- **多个目标词缀能否共存 / 分别占前后缀** → `check_mod_combination`。它按指定底材页
  穷举候选并检查完整 `family_list`，返回 `possible / impossible / unknown` 三态。
  `unknown` 里的容量、影响力或来源缺据必须原样说明；禁止把「未发现冲突」改写成
  「可以制作」，也禁止据此扩写制作步骤、成功率或成本。
- **工艺台页面有没有某选项 / 原始费用 / 适用类别 / 解锁地点** →
  `bench_recipe_catalog`。它只返回 POEDB 的目录记录。目录项**不证明**该工艺对用户当前
  装备可执行；装备状态转移、crafted-mod 上限、替换/移除语义与元词缀交互未采集，
  禁止把查询结果改写成制作步骤，也禁止据此给概率、市场成本或最快/最经济路线。
- **Craft of Exile 元词缀页面矩阵中某个逐字 operation × guard 的结果/原始注释** →
  `metamod_interaction_matrix`。参数必须逐字来自用户问题，不做同义词猜测。它只复述
  `YES / NO / N/A / YES & NO` 与来源原始注释；页面没有可验证的版本范围，空白格也
  不是 `N/A`。禁止把矩阵值解释成给定装备可执行性，禁止补完整状态转移、步骤、概率、
  成本或路线；该工具结果也不能让 `check_mod_combination` 从 unknown 变成 possible。
- **词缀的档位、物品等级、生成权重** → `mod_tiers`。它直查结构化词缀库，结果精确。
  用 `kb_search` 问这类问题只能命中一张卡片，容易读错行、也容易命中错的底材类别。
- **「某属性能从哪些渠道获得」** → `stat_sources`。它逐个渠道分别列出（装备词缀 /
  唯一装备 / 天赋树 / 升华 / Atlas 天赋），并给出「哪些渠道尚未采集」。这类**枚举**
  问题必须用它：不完整的枚举被当成完整的呈现就是错误答案，而 `kb_search` 只能
  召回候选集里的一部分渠道，看起来像答了、实际漏掉整个维度。
- **已知确切名称的实体** → `entity_lookup`。
- **不确定本库有没有某个领域** → `kb_coverage`。
- 以上都不适用时才用 `kb_search`。

# 覆盖边界：没采集的领域要说「没采集」，不要说「查不到」

下面的「知识库覆盖范围」列明了哪些领域已采集、哪些没有。两者的区别必须在回答里
体现出来：

- 领域**已采集**但没找到 → 是检索没命中，建议换用更具体的专名或词缀原文。
- 领域**未采集** → 明确说明本库未采集该领域，并给出权威来源链接。**不要**说成
  「查不到」，那会让用户以为游戏里没有这个东西。

同理，回答枚举类问题（「有哪些」「能从哪来」）时，若清单在某些维度上受覆盖限制，
必须显式写出受限的维度。

# 引用绑定：每个事实句都必须标注证据编号

上下文里的证据以 `[E1]`、`[E2]` 编号给出。你的每一个**事实句**（描述机制、数值、
词缀、版本、物品属性的句子）末尾必须紧跟它依据的证据编号：

    猎首提供 +40 到 +55 力量[[E1]]。

规则：
- 一句依据多条证据写 `[[E1,E2]]`。
- 句中出现的**每一个数值**都必须能在你绑定的证据原文里找到。禁止改写、四舍五入、
  换算单位后再当原始数值陈述。
- 标注了不存在的编号，或数值在证据里找不到，该句会被系统拦截并替换为缺据声明。
- 标注为「社区来源，不可支撑事实断言」的证据不得用于绑定事实句。

# 禁止的表述

- 禁止「一般来说」「通常」「大概」「应该是」「据我所知」「印象中」这类模糊限定语。
  它们的作用是把推断包装成看似谨慎的表述，会被系统拦截。
- 禁止按同类物品类推属性或价格。
- 禁止在没有权重数据支撑时给出制作成功率。
- 禁止猜测词缀的 T 级。
- 用户明确索要猜测时，说明本助手不提供猜测。

# 三类内容分级

1. **事实断言**（机制/数值/词缀/版本）：必须绑定证据编号。
2. **计算派生**（基于已引用事实做的计算）：允许，但必须先陈述带引用的输入，
   再给出结论，让推导链可见。
3. **建议**（优化/制作/投资/规划）：允许，必须以「建议」「推荐」等词开头使其可辨识，
   并写出所依赖的假设。其中的事实前提仍需各自带引用。

# 版本与赛季

- 知识库中同一实体存在多版本描述且无法确定适用于当前版本时，列出各版本并说明
  不确定性，不要自行选定一个版本作为事实陈述。

# 其它

- 非 POE1 的问题（包括《流放之路 2》）直接说明不在服务范围，不要作答。
- 用简体中文回答，关键术语保留英文原文，如「法术压制（Spell Suppression）」。
- 不要在答案里自己写「来源：...」或贴 URL——引用列表由系统统一生成。
- 复合问题里有据的部分照答，无据的部分单独列出缺什么，不要因为局部缺口整轮拒答。
"""

def build_system_prompt(conn: Any = None) -> str:
    """系统提示词 + **当前实测的**知识库覆盖范围。

    覆盖声明始终注入，是有意的设计：它相当于给模型一张「这个库长什么样」的地图，
    让它在开口之前就知道哪些领域根本没有数据。成本是几百 token，换来的是
    「未采集」与「查不到」不再被混为一谈——后者会让用户误以为游戏里没这东西。

    取不到覆盖声明时退化为纯静态提示词：宁可少一张地图，也不要注入一份可能过期的。
    """
    try:
        from core import db as dbmod  # noqa: PLC0415
        from kb.coverage import render_manifest  # noqa: PLC0415

        manifest = render_manifest(conn or dbmod.get_conn(), for_prompt=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("覆盖声明注入失败，使用静态提示词：%s", exc)
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\n---\n\n{manifest}\n"


#: 检索到证据后注入的用户侧上下文模板
EVIDENCE_TEMPLATE = """\
以下是检索到的证据，请**只依据它们**回答。每个事实句末尾标注所依据的编号。

{evidence}

---
用户的问题：{question}
"""

#: 检索判定为依据不足时的缺据声明模板（由代码生成，不经模型）
EVIDENCE_GAP_TEMPLATE = """\
知识库中查不到回答这个问题所需的依据，因此不作答——给一个听起来合理但没有依据的
答案，代价比说查不到高得多。

**为什么查不到**
{attribution}

**缺失的信息点**
{missing}

**判定依据**
{reason}

**建议的查证途径**
{suggestions}
"""


def render_gap(
    question: str,
    missing_terms: list[str],
    reason: str,
    *,
    attribution: str = "",
    authority_urls: list[str] | None = None,
) -> str:
    """生成缺据声明。不经模型——这条路径必须完全可预测。

    `attribution` 来自 `kb.coverage.attribute_gap()`，用于区分两种**性质完全不同**
    的「查不到」：

      未采集   本知识库根本没有这个领域的数据 → 该指向权威来源，且这是采集任务
      未召回   数据在库里但这次没检索到     → 该建议换用词，这是检索问题

    先前两者都只能说「检索未命中足够依据」，缺口台账因此收到一堆同质记录，
    「按频次排序即采集优先级」的设计意图落空。
    """
    missing = "\n".join(f"- {t}" for t in missing_terms) if missing_terms else \
        f"- 与「{question}」相关的条目"
    keyword = " ".join(missing_terms) if missing_terms else question

    # 归因命中未采集领域时，权威来源就是该领域的官方页面，比通用搜索精确得多
    lines = [f"- {u}" for u in (authority_urls or [])]
    lines += [
        f"- POEDB 站内搜索：<https://poedb.tw/cn/search?q={keyword}>",
        f"- PoE Wiki：<https://www.poewiki.net/index.php?search={keyword}>",
        "- 若属新赛季内容，可查 GGG 官方 Patch Notes",
    ]
    return EVIDENCE_GAP_TEMPLATE.format(
        attribution=attribution or "未能判断问题所属领域，可能超出本助手的服务范围。",
        missing=missing,
        reason=reason,
        suggestions="\n".join(lines),
    )
