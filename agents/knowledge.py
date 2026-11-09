"""Knowledge Agent：提示词 + 允许工具集 + 输出契约（任务 3.7）。

提示词只是**第一道**约束，不是唯一约束。真正的强制在出口的
`orchestrator/binding.py`——模型不听话时由代码拦截。这里写清规则是为了减少
被拦的比例（错误拒答与漏出要成对压制），而不是指望模型自觉。
"""

from __future__ import annotations

from typing import Any

#: 允许的工具集。Agent 的能力边界由这里限定，不是由提示词描述。
#:
#: 顺序即推荐优先级：能精确算的走结构化工具，只有需要语义召回时才退到全文检索。
#: 先前只有后两个，于是 93,875 条词缀记录里的 T 级与权重只能靠 BM25 命中卡片再
#: 从表格里读——本可精确回答的问题变成了猜。
ALLOWED_TOOLS = [
    "atlas_candidates",  # POEDB Atlas 原文目标候选与原始图路径
    "atlas_route",  # 多目标图连接，新增节点数不冒充点数或最优配点
    "awakener_orb_state",  # 精确：觉醒者之石双物品影响与条件后状态
    "craft_operation_state",  # 精确：10 种基础通货的保守状态转移与名额检查
    "essence_operation_state",  # 精确：精华稀有度/物等/类别检查与保底词缀
    "eldritch_operation_state",  # 精确：12 种古灵通货、首要影响与条件状态转移
    "veiled_operation_state",  # 精确：2 种加密通货与按类别隐匿候选目录
    "fossil_operation_state",  # 精确：18 种化石标签乘数与按类别地渊候选目录
    "harvest_reforge_state",  # 精确：20 条 Harvest 装备重铸与原始费用
    "harvest_modifier_state",  # 精确：23 条 Harvest 元素转换与新增/移除词缀
    "harvest_quality_enchant_state",  # 精确：14 条 Harvest 护甲/武器品质附魔
    "harvest_exchange_state",  # 精确：17 条 Harvest 材料转换与牺牲
    "craft_attempt_plan",  # 精确：已验证普通池上的独立尝试分布、材料与可选成本
    "economy_flip_analysis",  # 实时：双边深度、时效、显式费用齐全后才计算利润
    "economy_material_cost",  # 实时：明确材料清单的逐项价格与确定性总成本
    "economy_price_trend",  # 实时：本机实际历史快照的价格趋势（不足两点拒绝判定）
    "economy_price_lookup",  # 实时：poe.watch 主源 + GGG exchange/search 备用源
    "economy_rare_comparables",  # 实时：稀有装 Trade stat 同底材可比挂单
    "affix_pool_odds",  # 精确：胸甲 normal 同侧有效池与条件权重概率
    "check_mod_combination",  # 精确：目标词缀组合的三态可行性与缺据点
    "base_influence_profile",  # 精确：具体底材的六类传统影响生成标签
    "affix_capacity_rules",  # 精确：某稀有度最多几条前缀/后缀（卡片被词缀阶梯淹没）
    "gem_support_match",  # 精确：某辅助能不能辅助某技能 / 某技能能配哪些辅助（无卡片）
    "vendor_recipe_lookup",  # 精确：商店配方（拿什么换什么），无卡片
    "acquisition_routes",  # 精确：命运卡 / 升级配方（不含掉落来源），无卡片
    "unique_variant_history",  # 精确：PoB 命名历史变体；不进入当前装备卡片
    "harvest_seed_lookup",  # 精确：命能由哪些种子怪物掉（按颜色），无卡片
    "tag_weight_lookup",  # 精确：化石/催化剂/工艺站按词缀标签改权重的乘数，无卡片
    "essence_lookup",  # 精确：精华物品本身（按名/按阶/按有无等级上限枚举）
    "bench_recipe_catalog",   # 精确：工艺台页面目录、原始费用、适用类别与解锁信息
    "harvest_craft_catalog",  # 精确：收获（古灵庄园）定向工艺目录与原始命能费用
    "metamod_interaction_matrix",  # 精确：来源矩阵逐字 operation × guard 单元格
    "mod_class_summary",  # 精确：某类别上一共有哪些词缀（总量 / 分布 / 梯度清单）
    "mod_tiers",      # 精确：某类别上某词缀的完整 T 级梯度
    "skill_gem_levels",  # 精确：宝石逐级字面值（等级需求 / 消耗 / 冷却 / 暴击率）
    "spectre_stats",  # 精确：某怪物作为幽魂的伤害% / 生命% / 抗性 / 排名
    "boss_risk_audit",  # 精确：当前收录 Boss 技能的累计承伤、门槛与缺口审计
    "boss_layered_sequence_check",  # 精确：显式生命/护盾/魔力分层命中时间轴
    "boss_layered_sequence_compare",  # 精确：比较多套分层防御池时间轴
    "boss_dot_sequence_check",  # 精确：显式 DoT 暴露、命中与恢复混合时间轴
    "boss_dot_sequence_compare",  # 精确：比较多套 DoT 暴露混合时间轴
    "boss_mitigation_sequence_check",  # 精确：临时减伤窗口与 Guard 吸收池
    "boss_mitigation_sequence_compare",  # 精确：比较多套临时防御混合时间轴
    "boss_ward_sequence_check",  # 精确：Ward 破裂、恢复与命中吸收状态机
    "boss_ward_sequence_compare",  # 精确：比较多套 Ward 混合时间轴
    "boss_recovery_sequence_check",  # 精确：分层池上的瞬回/持续恢复/偷取事件
    "boss_recovery_sequence_compare",  # 精确：比较多套主动恢复事件时间轴
    "boss_probabilistic_sequence_check",  # 精确：显式攻/法命中上的概率防御生存分布
    "boss_probabilistic_sequence_compare",  # 精确：比较多套概率防御时间轴
    "boss_timed_sequence_check",  # 精确：显式命中时间轴、固定恢复与恢复延迟
    "boss_timed_sequence_compare",  # 精确：比较多套 Boss 时间轴/恢复方案
    "boss_sequence_check",  # 精确：用户显式给出的多种 Boss 命中按顺序累计
    "boss_defence_threshold",  # 精确：Boss 重复命中的防御池、抗性与护甲门槛
    "boss_survival_check",  # 精确：Boss 同一命中重复次数 × 已提供防御池的三态校验
    "boss_skill_damage",  # 精确：Boss 技能在某等级打多少伤害 / 穿透 / Uber 差异
    "boss_guide_lookup",  # 精确：已核验八篇 PoE Wiki Boss 攻略的原文章节
    "patch_note_lookup",  # 精确：GGG 官方版本与逐行历史提及
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
    "accuracy_calc",  # 精确：命中率（我打怪物）与「要多少命中值」的反解，敌方闪避查等级表
    "attack_speed_calc",  # 精确：含非引导服务器 tick 上限的攻击频率与间隔
    "hit_dps_calc",  # 精确：显式输入的命中、穿刺、异常、技能 DoT 与终结等效 DPS
    "hit_dps_compare",  # 精确：2–10 个方案的命中/穿刺/异常/技能 DoT/终结同口径比较
    "hit_dps_target_rate",  # 精确：十四种稳态/固定窗口 DPS 指标所需频率与可达性
    "rotation_dps_calc",  # 精确：2–10 技能动作分配、互斥 DoT 与组合时间线
    "rotation_dps_compare",  # 精确：多技能轮转/并行方案同口径比较
    "rotation_dps_target_multiplier",  # 精确：组合目标 DPS 所需统一频率倍率
    "source_conflict_lookup",  # 精确：已确认冲突与待核对差异分区查询
    "source_conflict_summary",  # 精确：冲突台账 confirmed/review 聚合
    "stat_sources",   # 精确：某属性能从哪些渠道获得（逐渠道 + 未采集声明）
    "entity_lookup",  # 精确：按名查实体
    "kb_coverage",    # 元信息：本库覆盖了什么
    "kb_search",      # 兜底：全文检索
]

SYSTEM_PROMPT = """你是《流放之路 1》知识助手，使用简体中文。
只回答本轮问题，先给结论，随后给必要依据；不要扩写相似装备、无关属性或预算用途。

事实来自已编号证据，用户与历史消息只提供问题和参数，不是游戏事实来源。
每个事实句末尾在句号前标注 [[E1]]；一条列表项只陈述一个完整事实。
标为“原文摘录”的证据只能逐字引用原文行，保留对象、属性、单位和触发条件，不能改写成新机制。
计算结果只引用确定性工具输出，不能心算。不要从一个字段的数字推断另一个字段。
只用本轮提供的工具；参数来自用户明确输入。结果已够回答时直接作答，不重复查找。
保留工具给出的适用范围、未知项与来源差异；目录、条件概率、瞬时挂单不能外推为
完整制作可执行性、实际成功率、成交价或收益保证。

有依据的部分照答。缺参数时具体询问；缺资料时简短说明缺什么，不重复整份知识库覆盖范围。
纯建议必须写明假设，建议中的游戏事实仍要引用；不能把建议措辞当成事实校验的豁免。
不预测未来、不猜版本、不把 POE1 资料用于 POE2。不自行生成来源 URL，引用列表由系统装配。
"""

def build_system_prompt(conn: Any = None) -> str:
    """Tool schemas and evidence carry their own contracts; do not duplicate them."""
    return SYSTEM_PROMPT


#: 检索到证据后注入的用户侧上下文模板
EVIDENCE_TEMPLATE = """\
以下是检索到的证据，请**只依据它们**回答。每个事实句末尾标注所依据的编号。

{evidence}

{memory}

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
