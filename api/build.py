"""`/build`：PoB、官方天赋树与官方角色 API 导入（需求 8.1 的入口）。

路由层只做校验与编排：PoB、天赋树、角色 API 与用户确认后的截图字段分别由对应
解析器转换为同一个 `PobBuild`，之后统一进入报告链路。

**本模块的分析链路不调用 LLM**（截图 OCR 在独立的 `/screenshot/recognize`）：输出是代码装配的，
同一份分享码永远得到同一份结果。也正因如此它不过出口绑定闸门——
闸门防的是模型编造，这里没有模型。

需求 8.4 的 16 项体检、8.5 的防御补充、8.6 的三类输出分析、8.7 的
双档优化建议、V3 的显式目标成长里程碑与多方案 PlayerStat 横向比较已经接入，
但遵守「**只判不算**」边界：值只取输入本身携带的字段，本库只用有出处的
常量与规则判读；缺失项标为「未知」，不会估计。因此返回里
`analysis_done=true` 表示这些项目均已检查，**不表示本库重算了 Build**。输出分析中，
单体只对应当前 Calcs 技能，清图只列覆盖指标，Boss 必须有显式敌人档位。
防御来源只拆装备文本中能逐字核对的部分；优化建议只由已知且低于明确上限的指标触发。
需求 8.9 的版本核对读取对应输入携带的版本字段，与本地数据表版本比较。

`player_stats` 那一段**是 PoB 自己算的快照**，不是本库算的，也不是知识库证据——
它反映用户自己那份 PoB 的版本与配置。字段名与 `source_note` 都写清这一点，
免得下游把它与本库计算结果混为同一来源。只有显式请求 `boss_survival`、
`boss_sequence`、`boss_timed_sequence` 或 `boss_risk_audit` 时，本库才把白名单字段当作
已标注来源的输入送入单击、用户显式混合序列、带恢复时间轴或横向风险审计计算器，并逐字段
回传实际采用值。
混合序列始终使用基础防御字段，不会把 PoB 的单击 ``*MaximumHitTaken`` 拆开复用。
"""

from __future__ import annotations

import logging
import math
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from api.deps import require_user
from api.boss import (
    BossLayeredEvent,
    BossLayeredSequenceResponse,
    BossProbabilisticEvent,
    BossProbabilisticSequenceResponse,
    BossRiskAuditResponse,
    BossSequenceEntry,
    BossSequenceResponse,
    BossSurvivalResponse,
    BossTimedEvent,
    BossTimedSequenceResponse,
)
from core import db as dbmod
from core.config import get_config, get_secrets
from kb.fetcher import Fetcher, Poe2SourceRejected
from parsers.build_advice import build_suggestions
from parsers.build_compare import BuildScenarioComparison, compare_build_scenarios
from parsers.build_growth import (
    GROWTH_METRICS,
    BuildGrowthPlan,
    GrowthObjective,
    plan_build_growth,
)
from parsers.build_report import build_health_report
from parsers.build_sources import build_defence_sources
from parsers.build_version import check_build_version
from parsers.character_api import CharacterApiClient, CharacterApiError, character_build
from parsers.pob_build import PobParseError, bridge_gems, decode_share_code, parse_build
from parsers.pob_link import PobLinkError, looks_like_link, resolve_download_url
from parsers.passive_tree_link import (
    PassiveTreeLinkError,
    looks_like_passive_tree_link,
    passive_tree_build,
)
from parsers.screenshot import (
    NUMERIC_STAT_KEYS,
    TEXT_STAT_KEYS,
    ScreenshotBuildFields,
    screenshot_build,
)
from tools.impl.boss import (
    boss_risk_audit_from_build,
    boss_sequence_from_build,
    boss_survival_from_build,
)
from tools.impl.boss_timeline import boss_timed_sequence_from_build
from tools.impl.boss_probability import boss_probabilistic_sequence_from_build
from tools.impl.boss_layered import boss_layered_sequence_from_build

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Build"])


class BuildBossSurvivalRequest(BaseModel):
    """用完整 Max Hit 或基础防御快照校验 Boss 重复命中。"""

    skill: str = Field(min_length=2, max_length=120)
    level: int = Field(ge=1, le=100)
    uber: bool = False
    hit_count: int = Field(default=1, ge=1, le=100)
    roll_range: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    use_pob_max_hit: bool = Field(
        default=True,
        description="配置完全匹配时优先使用 PoB 导出的完整 Max Hit 快照。",
    )
    pool_mode: Literal["life", "life_and_energy_shield"] = Field(
        default="life_and_energy_shield",
        description="基础字段降级模式使用的防御池；不改变 PoB Max Hit 快照本身的口径。",
    )


class BuildBossSequenceRequest(BaseModel):
    """用 Build 的基础 PlayerStat 快照校验用户显式排列的混合命中。"""

    sequence: list[BossSequenceEntry] = Field(min_length=1, max_length=100)
    level: int = Field(ge=1, le=100)
    uber: bool = False
    roll_range: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    pool_mode: Literal["life", "life_and_energy_shield"] = Field(
        default="life_and_energy_shield",
        description="从 Build 快照采用未保留生命，或未保留生命加能量护盾。",
    )

    @model_validator(mode="after")
    def expanded_sequence_limit(self) -> "BuildBossSequenceRequest":
        if sum(item.count for item in self.sequence) > 100:
            raise ValueError("展开后的命中序列最多包含 100 次命中")
        return self


class BuildBossTimedSequenceRequest(BaseModel):
    """用 Build 基础防御快照推进用户显式标时的命中与恢复。"""

    events: list[BossTimedEvent] = Field(min_length=1, max_length=100)
    level: int = Field(ge=1, le=100)
    uber: bool = False
    roll_range: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    pool_mode: Literal["life", "life_and_energy_shield"] = Field(
        default="life",
        description="默认只用未保留生命；生命+护盾为分立池，自动恢复无法安全合并。",
    )
    recovery_per_second: float | None = Field(
        default=None, ge=0, le=1_000_000_000, allow_inf_nan=False,
        description=(
            "省略时 life 模式按 NetLifeRegen/LifeRegenRecovery/LifeRegen 顺序取快照；"
            "life_and_energy_shield 模式必须显式提供共享恢复值。"
        ),
    )
    recovery_delay_seconds: float = Field(
        default=0, ge=0, le=3600, allow_inf_nan=False
    )


class BuildBossProbabilisticSequenceRequest(BaseModel):
    """用 Build 基础防御与概率字段推进显式 attack/spell 时间轴。"""

    events: list[BossProbabilisticEvent] = Field(min_length=1, max_length=100)
    level: int = Field(ge=1, le=100)
    uber: bool = False
    roll_range: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    pool_mode: Literal["life", "life_and_energy_shield"] = Field(
        default="life",
        description="默认只用未保留生命；组合池仍要求显式共享恢复。",
    )
    recovery_per_second: float | None = Field(
        default=None, ge=0, le=1_000_000_000, allow_inf_nan=False
    )
    recovery_delay_seconds: float = Field(
        default=0, ge=0, le=3600, allow_inf_nan=False
    )
    attack_hit_chance_pct: float | None = Field(
        default=None, ge=0, le=100, allow_inf_nan=False,
        description="省略时尝试从 EvadeChance/EvasionChance 取 100%-闪避率。",
    )
    attack_block_chance_pct: float | None = Field(
        default=None, ge=0, le=100, allow_inf_nan=False
    )
    spell_block_chance_pct: float | None = Field(
        default=None, ge=0, le=100, allow_inf_nan=False
    )
    spell_suppression_chance_pct: float | None = Field(
        default=None, ge=0, le=100, allow_inf_nan=False
    )
    blocked_damage_taken_pct: float = Field(
        default=0, ge=0, le=100, allow_inf_nan=False
    )
    suppressed_damage_taken_pct: float = Field(
        default=60, ge=0, le=100, allow_inf_nan=False
    )
    bucket_size: float | None = Field(
        default=None, gt=0, le=1_000_000_000, allow_inf_nan=False
    )


class BuildBossLayeredSequenceRequest(BaseModel):
    """从 Build PlayerStat 推进生命、护盾与可选魔力分层时间轴。"""

    events: list[BossLayeredEvent] = Field(min_length=1, max_length=100)
    level: int = Field(ge=1, le=100)
    uber: bool = False
    roll_range: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    mana_before_life_pct: float = Field(
        default=0, ge=0, le=100, allow_inf_nan=False,
        description="必须显式提供；不从职业、天赋或其它 PlayerStat 猜 MoM 比例。",
    )
    life_recovery_per_second: float | None = Field(
        default=None, ge=0, le=1_000_000_000, allow_inf_nan=False
    )
    energy_shield_recovery_per_second: float | None = Field(
        default=None, ge=0, le=1_000_000_000, allow_inf_nan=False
    )
    mana_recovery_per_second: float | None = Field(
        default=None, ge=0, le=1_000_000_000, allow_inf_nan=False
    )
    energy_shield_recharge_per_second: float | None = Field(
        default=None, ge=0, le=1_000_000_000, allow_inf_nan=False
    )
    energy_shield_recharge_delay_seconds: float = Field(
        default=2, ge=0, le=3600, allow_inf_nan=False
    )
    energy_shield_recharge_interrupt_mode: Literal[
        "energy_shield_damage", "any_hit"
    ] = "energy_shield_damage"


class BuildBossRiskAuditRequest(BaseModel):
    """用 Build 基础快照横向审计当前收录的 Boss 连续命中与防御缺口。"""

    level: int = Field(ge=1, le=100)
    hit_count: int = Field(default=1, ge=1, le=100)
    uber: bool = False
    roll_range: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    pool_mode: Literal["life", "life_and_energy_shield"] = Field(
        default="life_and_energy_shield",
        description="从 Build 快照采用未保留生命，或未保留生命加能量护盾。",
    )


GrowthMetric = Literal[
    "life",
    "energy_shield",
    "fire_resistance",
    "cold_resistance",
    "lightning_resistance",
    "chaos_resistance",
    "spell_suppression",
    "attack_block",
    "spell_block",
    "armour",
    "evasion",
    "life_recovery",
    "dps",
    "physical_max_hit",
    "fire_max_hit",
    "cold_max_hit",
    "lightning_max_hit",
    "chaos_max_hit",
]


class BuildGrowthObjectiveRequest(BaseModel):
    """一个用户显式目标；priority 数字越小越优先。"""

    metric: GrowthMetric
    target: float = Field(allow_inf_nan=False)
    priority: int = Field(default=50, ge=1, le=100)
    label: str = Field(default="", max_length=80)

    @field_validator("target", mode="before")
    @classmethod
    def reject_boolean_target(cls, value):  # noqa: ANN001
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("成长目标必须是 JSON number，不能是字符串或布尔值")
        return value

    @model_validator(mode="after")
    def validate_metric_target(self) -> "BuildGrowthObjectiveRequest":
        spec = GROWTH_METRICS[self.metric]
        if abs(self.target) > 1_000_000_000_000_000:
            raise ValueError("成长目标超出安全数值范围")
        if self.target < 0 and not spec.allow_negative_target:
            raise ValueError(f"{self.metric} 的目标不能为负数")
        self.label = self.label.strip()
        return self


class BuildGrowthPlanRequest(BaseModel):
    objectives: list[BuildGrowthObjectiveRequest] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_metrics(self) -> "BuildGrowthPlanRequest":
        metrics = [item.metric for item in self.objectives]
        if len(set(metrics)) != len(metrics):
            raise ValueError("同一个成长指标不能重复")
        return self


class BuildScenarioRequest(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    code: str = Field(
        min_length=1,
        max_length=200000,
        description="PoB 分享码或受支持的白名单托管链接。",
    )

    @model_validator(mode="after")
    def normalize(self) -> "BuildScenarioRequest":
        self.label = self.label.strip()
        self.code = self.code.strip()
        if not self.label or not self.code:
            raise ValueError("方案 label 与 code 不能为空")
        return self


class BuildCompareRequest(BaseModel):
    scenarios: list[BuildScenarioRequest] = Field(min_length=2, max_length=5)
    reference_label: str = Field(default="", max_length=80)
    metrics: list[GrowthMetric] | None = Field(default=None, min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_scenarios(self) -> "BuildCompareRequest":
        labels = [item.label for item in self.scenarios]
        if len(set(labels)) != len(labels):
            raise ValueError("方案 label 不能重复")
        self.reference_label = self.reference_label.strip()
        if self.reference_label and self.reference_label not in labels:
            raise ValueError("reference_label 必须对应一个已提交方案")
        if self.metrics is not None and len(set(self.metrics)) != len(self.metrics):
            raise ValueError("同一个比较指标不能重复")
        return self


class BuildRequest(BaseModel):
    code: str = Field(
        default="", max_length=200000,
        description=(
            "PoB 的分享码、**已知分享站**的链接，或 pathofexile.com 官方天赋树链接"
            "（pobb.in / pastebin.com / pob.codes / poe.ninja / Maxroll / rentry.co …）。"
            "托管链接只认白名单站点并由服务端抓取；官方天赋树链接只做本地解码。"
        ),
    )
    character_name: str = Field(
        default="", max_length=100,
        description="当前 POE_OAUTH_TOKEN 所属账号中的 PoE1 PC 角色名。",
    )
    boss_survival: BuildBossSurvivalRequest | None = Field(
        default=None,
        description=(
            "可选 Boss 单击或同一命中重复校验。配置匹配时使用导入 Build 的完整 Max Hit；"
            "否则降级到 PlayerStat 基础防御快照；连续多击不使用单击 Max Hit。"
            "缺字段或机制不明时返回 unknown。"
        ),
    )
    boss_sequence: BuildBossSequenceRequest | None = Field(
        default=None,
        description=(
            "可选的用户指定 Boss 混合命中序列。从 PlayerStat 读取池子、各伤害类型抗性与"
            "物理护甲；始终不使用 PoB 单击 MaximumHitTaken。"
        ),
    )
    boss_timed_sequence: BuildBossTimedSequenceRequest | None = Field(
        default=None,
        description=(
            "可选的用户指定 Boss 命中时间轴。从 PlayerStat 读取基础防御；life 模式可读取"
            "净生命回复，或由调用方显式给共享恢复速率。不会使用 PoB 单击 Max Hit。"
        ),
    )
    boss_probabilistic_sequence: BuildBossProbabilisticSequenceRequest | None = Field(
        default=None,
        description=(
            "可选的显式 attack/spell Boss 时间轴概率分布。从 PlayerStat 读取适用的闪避、"
            "格挡和压制字段，或采用请求中的显式覆盖；同时保留确定性最坏轨迹。"
        ),
    )
    boss_layered_sequence: BuildBossLayeredSequenceRequest | None = Field(
        default=None,
        description=(
            "可选的生命/能量护盾/魔力分层 Boss 时间轴。逐击护盾绕过和 MoM 比例必须"
            "显式提供；池与回复优先取 PlayerStat，缺字段时返回 unknown。"
        ),
    )
    boss_risk_audit: BuildBossRiskAuditRequest | None = Field(
        default=None,
        description=(
            "可选的 Boss 命中风险与防御缺口审计。按最坏累计承伤排列本地已收录技能，"
            "给出池子及抗性/护甲目标；缺字段或模型不覆盖的技能保留为 unknown。"
        ),
    )
    growth_plan: BuildGrowthPlanRequest | None = Field(
        default=None,
        description=(
            "可选的 Build 成长目标。只比较用户目标与当前 PlayerStat，按显式优先级输出"
            "里程碑；不推断具体装备、工艺、价格或目标可达性。"
        ),
    )

    @model_validator(mode="after")
    def exactly_one_input(self) -> "BuildRequest":
        supplied = bool(self.code.strip()) + bool(self.character_name.strip())
        if supplied != 1:
            raise ValueError("code 与 character_name 必须且只能提供一个")
        return self


class ScreenshotBuildRequest(BaseModel):
    """用户确认后的截图可见字段。

    这是浏览器回传的普通用户输入，不能因为最初来自视觉模型就信任；键名与数值范围
    在这里重新校验。`confirmed` 把“先确认再分析”落实成 API 契约。
    """

    confirmed: bool = False
    level: int | None = Field(default=None, ge=1, le=100)
    class_name: str = Field(default="", max_length=100)
    ascend_class_name: str = Field(default="", max_length=100)
    main_skill: str = Field(default="", max_length=200)
    player_stats: dict[str, float] = Field(default_factory=dict, max_length=80)
    player_stat_text: dict[str, str] = Field(default_factory=dict, max_length=20)
    enemy_is_boss: str = Field(default="", max_length=20)
    growth_plan: BuildGrowthPlanRequest | None = None

    @field_validator("player_stats", mode="before")
    @classmethod
    def reject_non_numeric_stats(cls, value):  # noqa: ANN001
        if isinstance(value, dict) and any(
            isinstance(item, bool) or not isinstance(item, (int, float))
            for item in value.values()
        ):
            raise ValueError("截图数值字段必须是 JSON number，不能是字符串或布尔值")
        return value

    @model_validator(mode="after")
    def validate_confirmed_fields(self) -> "ScreenshotBuildRequest":
        if not self.confirmed:
            raise ValueError("截图识别结果必须经用户确认后才能进入 Build 体检")
        bad_numeric = sorted(set(self.player_stats) - NUMERIC_STAT_KEYS)
        bad_text = sorted(set(self.player_stat_text) - TEXT_STAT_KEYS)
        if bad_numeric or bad_text:
            raise ValueError(f"截图包含不受支持的字段：{bad_numeric + bad_text}")
        if any(
            not math.isfinite(value) or abs(value) > 1_000_000_000_000_000
            for value in self.player_stats.values()
        ):
            raise ValueError("截图数值必须是合理范围内的有限数字")
        if any(len(value) > 100 for value in self.player_stat_text.values()):
            raise ValueError("截图文本指标超过 100 字符")
        if self.enemy_is_boss not in {"", "Boss", "Pinnacle", "Uber", "Shaper", "Sirus"}:
            raise ValueError("enemy_is_boss 不是受支持的 PoB 敌人档位")
        if not any((
            self.level,
            self.class_name.strip(),
            self.ascend_class_name.strip(),
            self.main_skill.strip(),
            self.player_stats,
            self.player_stat_text,
            self.enemy_is_boss,
        )):
            raise ValueError("截图没有任何可确认的 Build 字段")
        return self


class GemOut(BaseModel):
    name_en: str
    name_zh: str = ""
    level: int | None = None
    quality: int | None = None
    enabled: bool = True
    #: 未桥接时为 None——**保留英文原文，不猜**
    gem_id: str | None = None


class SkillGroupOut(BaseModel):
    slot: str = ""
    enabled: bool = True
    is_main: bool = False
    gems: list[GemOut] = []


class ItemOut(BaseModel):
    slot: str = ""
    name: str = ""
    base_type: str = ""
    rarity: str = ""
    item_level: int | None = None
    implicits: list[str] = []
    explicits: list[str] = []
    error: str = ""


class CheckOut(BaseModel):
    key: str
    label: str
    value: str = ""
    known: bool = True
    missing_reason: str = ""
    verdict: str = ""
    basis: str = ""
    source: str = ""


class DefenceSourceOut(BaseModel):
    category: str
    label: str
    slot: str = ""
    item_name: str = ""
    origin: str
    detail: str


class BuildSuggestionOut(BaseModel):
    weakness: str
    tier: str
    action: str
    expected_gain: str
    estimated_cost: str
    assumptions: str


class BuildVersionOut(BaseModel):
    build_version: str = ""
    system_version: str = ""
    known: bool = False
    matches: bool | None = None
    message: str = ""
    basis: str = ""


class BuildBossCitationOut(BaseModel):
    chunk_id: str
    entity_id: str | None = None
    title: str
    url: str
    source: str
    source_priority: int
    can_support_facts: bool
    text: str
    channel: str = "derived"


class BuildBossSurvivalOut(BossSurvivalResponse):
    pool_mode: Literal["life", "life_and_energy_shield"]
    input_source: str = "pob_player_stat"
    source_fields: dict[str, float] = {}
    source_config: dict[str, str] = {}
    max_hit_field: str = ""
    max_hit_field_note: str = ""
    calculation_mode: str | None = None
    fallback_reason: str = ""
    citations: list[BuildBossCitationOut] = []


class BuildBossSequenceOut(BossSequenceResponse):
    pool_mode: Literal["life", "life_and_energy_shield"]
    input_source: str = "pob_player_stat"
    source_fields: dict[str, float] = {}
    source_config: dict[str, str] = {}
    calculation_mode: str | None = None
    fallback_reason: str = ""
    citations: list[BuildBossCitationOut] = []


class BuildBossTimedSequenceOut(BossTimedSequenceResponse):
    pool_mode: Literal["life", "life_and_energy_shield"]
    input_source: str = "pob_player_stat"
    source_fields: dict[str, float] = {}
    source_config: dict[str, str] = {}
    calculation_mode: str | None = None
    fallback_reason: str = ""
    citations: list[BuildBossCitationOut] = []


class BuildBossProbabilisticSequenceOut(BossProbabilisticSequenceResponse):
    pool_mode: Literal["life", "life_and_energy_shield"]
    input_source: str = "pob_player_stat"
    source_fields: dict[str, float] = {}
    source_config: dict[str, str] = {}
    calculation_mode: str | None = None
    fallback_reason: str = ""
    citations: list[BuildBossCitationOut] = []


class BuildBossLayeredSequenceOut(BossLayeredSequenceResponse):
    input_source: str = "pob_player_stat"
    source_fields: dict[str, float] = {}
    source_config: dict[str, str] = {}
    calculation_mode: str | None = None
    fallback_reason: str = ""
    citations: list[BuildBossCitationOut] = []


class BuildBossRiskAuditOut(BossRiskAuditResponse):
    pool_mode: Literal["life", "life_and_energy_shield"]
    input_source: str = "pob_player_stat"
    source_fields: dict[str, float] = {}
    calculation_mode: str | None = None
    fallback_reason: str = ""
    citations: list[BuildBossCitationOut] = []


class BuildGrowthMilestoneOut(BaseModel):
    sequence: int
    metric: GrowthMetric
    label: str
    priority: int
    status: Literal["met", "gap", "unknown"]
    current: float | None = None
    target: float
    gap: float | None = None
    unit: str = ""
    source_field: str = ""
    current_source: str
    target_source: str
    action: str
    expected_gain: str = ""
    verification: str
    assumptions: str


class BuildGrowthPlanOut(BaseModel):
    status: Literal["ready", "partial", "complete", "unknown"]
    total: int
    met: int
    remaining: int
    unknown: int
    completion_percent: float
    next_metric: str = ""
    milestones: list[BuildGrowthMilestoneOut] = []
    note: str


class BuildScenarioSnapshotOut(BaseModel):
    label: str
    level: int | None = None
    class_name: str
    ascend_class_name: str
    active_skill: str
    enemy_context: str
    tree_version: str
    player_stat_count: int


class BuildScenarioContextOut(BaseModel):
    label: str
    status: Literal["reference", "ready", "mismatch", "unknown"]
    reasons: list[str]
    differing_config_keys: list[str]


class BuildScenarioStructureDiffOut(BaseModel):
    label: str
    status: Literal["reference", "ready", "partial"]
    changed_item_slots: list[str]
    added_gems: list[str]
    removed_gems: list[str]
    passive_nodes_added: list[str]
    passive_nodes_removed: list[str]
    note: str


class BuildScenarioMetricValueOut(BaseModel):
    label: str
    status: Literal["reference", "ready", "unknown"]
    value: float | None = None
    source_field: str
    delta: float | None = None
    delta_percent: float | None = None
    direction: Literal["baseline", "up", "down", "equal", "unknown"]
    reason: str


class BuildMetricComparisonOut(BaseModel):
    metric: GrowthMetric
    label: str
    unit: str
    status: Literal["ready", "partial", "unknown"]
    values: list[BuildScenarioMetricValueOut]


class BuildCompareResponse(BaseModel):
    status: Literal["ready", "partial", "unknown"]
    reference_label: str
    scenario_count: int
    context_status: Literal["ready", "mismatch", "unknown"]
    ready_metrics: int
    partial_metrics: int
    unknown_metrics: int
    scenarios: list[BuildScenarioSnapshotOut]
    context_checks: list[BuildScenarioContextOut]
    structure_diffs: list[BuildScenarioStructureDiffOut]
    metrics: list[BuildMetricComparisonOut]
    overall_verdict: Literal["not_ranked"]
    note: str


class BuildResponse(BaseModel):
    input_type: str = "pob"
    level: int | None = None
    class_name: str = ""
    ascend_class_name: str = ""
    bandit: str = ""
    pantheon_major: str = ""
    pantheon_minor: str = ""
    skill_groups: list[SkillGroupOut] = []
    items: list[ItemOut] = []
    config: dict[str, str] = {}
    #: **PoB 自己算的快照**，见 `player_stats_note`
    player_stats: dict[str, float] = {}
    player_stats_note: str = ""
    #: 可选：用当前 Build 的完整 Max Hit 或基础防御快照校验 Boss 命中
    boss_survival: BuildBossSurvivalOut | None = None
    #: 可选：用当前 Build 的基础防御快照校验用户显式排列的混合 Boss 命中
    boss_sequence: BuildBossSequenceOut | None = None
    #: 可选：用当前 Build 基础防御与净回复推进显式标时的 Boss 命中
    boss_timed_sequence: BuildBossTimedSequenceOut | None = None
    #: 可选：用当前 Build 概率防御字段计算显式 attack/spell 时间轴生存分布
    boss_probabilistic_sequence: BuildBossProbabilisticSequenceOut | None = None
    #: 可选：分别推进生命、能量护盾、魔力与护盾回充
    boss_layered_sequence: BuildBossLayeredSequenceOut | None = None
    #: 可选：按当前 Build 基础防御快照横向审计已收录的 Boss 单击
    boss_risk_audit: BuildBossRiskAuditOut | None = None
    #: 可选：用户显式目标与当前 PlayerStat 之间的确定性成长里程碑
    growth_plan: BuildGrowthPlanOut | None = None
    #: 需求 8.4 的逐项体检。**只判不算**：值取自 build，判据取自本库常量
    report: list[CheckOut] = []
    #: 需求 8.6：单体 / 清图 / Boss 分开，且显式绑定当前 PoB 配置
    output_report: list[CheckOut] = []
    #: 装备文本中可逐字核对的防御来源；不重算、不冒充完整 PoB breakdown
    defence_sources: list[DefenceSourceOut] = []
    defence_sources_unresolved: list[str] = []
    defence_sources_note: str = ""
    suggestions: list[BuildSuggestionOut] = []
    suggestions_note: str = ""
    version_check: BuildVersionOut
    tree_serialization_version: int | None = None
    ascendancy_id: int | None = None
    secondary_ascendancy_id: int | None = None
    mastery_effects: dict[str, str] = {}
    character_name: str = ""
    character_league: str = ""
    character_realm: str = ""
    alternate_ascendancy_name: str = ""
    report_notes: list[str] = []
    analysis_done: bool = True
    analysis_note: str = ""
    warnings: list[str] = []


_STATS_NOTE = (
    "以上数值取自你粘贴的 build 自带的 <PlayerStat>，是 **PoB 自己算的**，"
    "既不是本库的计算结果、也不是知识库证据——它反映你那份 PoB 的版本与配置。"
    "本库不会把它冒充自己的重算值；若显式请求 Boss 命中校验，响应会另列实际采用字段。"
)
_ANALYSIS_NOTE = (
    "需求 8.4 的 16 项、8.5 的防御补充与 8.6 的输出场景已逐项核对，"
    "但**本库没有重算任何一项**："
    "数值取自你的 build，本库只做判读；缺的一律标「未知」而不填估计值。"
    "**DPS 只是复述你 PoB 的数**——本库缺整条加成链，算不了 DPS。"
)
_CHARACTER_ANALYSIS_NOTE = (
    "官方角色 API 提供角色身份、已装备物品、插槽宝石、珠宝与天赋，但不提供 PoB 的"
    "PlayerStat 或配置快照；因此可核对输入结构，DPS、EHP、Max Hit、抗性等最终面板"
    "保持未知，不从局部数据估算。"
)
_SCREENSHOT_STATS_NOTE = (
    "以上数值取自你确认的截图转录。视觉模型只负责 OCR，**不是 PoB 计算结果**；"
    "截图未显示的数值不会补全。"
)
_SCREENSHOT_ANALYSIS_NOTE = (
    "需求 8.4 的 16 项、8.5 的防御补充与 8.6 的输出场景均已逐项核对；"
    "本轮只使用你确认的截图可见字段。未拍到、看不清或无法精确映射的项目均为“未知”，"
    "不从职业、技能或局部面板推算完整 Build。"
)


def _check_out(check) -> CheckOut:  # noqa: ANN001
    return CheckOut(
        key=check.key, label=check.label, value=check.value, known=check.known,
        missing_reason=check.missing_reason, verdict=check.verdict,
        basis=check.basis, source=check.source,
    )


def _resolve_code(raw: str) -> str:
    """输入既可能是分享码、也可能是链接。是链接就先抓回原文。

    抓取复用 `kb.fetcher.Fetcher`（限速、robots、缓存、UA 一并继承），
    它内部还会再过一次 `reject_poe2_host`——白名单之外的第二道。
    """
    if not looks_like_link(raw):
        return raw
    url = resolve_download_url(raw)
    cfg = get_config()
    fetcher = Fetcher(
        cache_dir=cfg.cache_dir,
        user_agent=str(cfg.get("crawl.user_agent")),
        requests_per_second=float(cfg.get("crawl.requests_per_second", 0.5)),
        timeout=float(cfg.get("crawl.timeout_seconds", 60)),
        cache_ttl_hours=int(cfg.get("crawl.cache_ttl_hours", 24)),
    )
    logger.info("build 链接导入 %s → %s", raw, url)
    return fetcher.fetch(url).text.strip()


def _character_client() -> CharacterApiClient:
    cfg = get_config()
    secrets = get_secrets()
    return CharacterApiClient(
        token=secrets.poe_oauth_token,
        user_agent=secrets.poe_oauth_user_agent,
        timeout=float(cfg.get("crawl.timeout_seconds", 60)),
    )


def _raise_character_error(exc: CharacterApiError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"error": exc.code, "reason": exc.reason},
    ) from exc


class CharacterSummaryOut(BaseModel):
    name: str
    class_name: str = ""
    level: int | None = None
    league: str = ""
    realm: str = "pc"


class CharacterListResponse(BaseModel):
    characters: list[CharacterSummaryOut] = []
    source: str = "GGG official API"


@router.get("/build/characters", response_model=CharacterListResponse)
def list_characters(caller: str = Depends(require_user)) -> CharacterListResponse:
    try:
        entries = _character_client().list_characters()
    except CharacterApiError as exc:
        _raise_character_error(exc)
    characters = [
        CharacterSummaryOut(
            name=str(entry.get("name") or ""),
            class_name=str(entry.get("class") or ""),
            level=entry.get("level") if isinstance(entry.get("level"), int) else None,
            league=str(entry.get("league") or ""),
            realm=str(entry.get("realm") or "pc"),
        )
        for entry in entries
        if entry.get("name") and str(entry.get("realm") or "pc") == "pc"
    ]
    logger.info("角色列表 caller=%s PoE1 PC 角色=%d", caller, len(characters))
    return CharacterListResponse(characters=characters)


@router.post("/build", response_model=BuildResponse)
def analyze_build(req: BuildRequest, caller: str = Depends(require_user)) -> BuildResponse:
    input_type = "pob"
    tree_link = None
    character_meta = None
    try:
        if req.character_name.strip():
            input_type = "character_api"
            payload = _character_client().get_character(req.character_name)
            build, character_meta = character_build(payload)
            build = bridge_gems(dbmod.get_conn(), build)
        elif looks_like_passive_tree_link(req.code):
            input_type = "passive_tree"
            build, tree_link = passive_tree_build(req.code)
        else:
            code = _resolve_code(req.code)
            xml = decode_share_code(code)
            build = bridge_gems(dbmod.get_conn(), parse_build(xml))
    except CharacterApiError as exc:
        _raise_character_error(exc)
    except PassiveTreeLinkError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "passive_tree_parse_failed", "reason": str(exc)},
        ) from exc
    except (PobLinkError, Poe2SourceRejected) as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "pob_link_rejected", "reason": str(exc)},
        ) from exc
    except OSError as exc:
        # 抓取失败是**外部**问题，与「链接不合法」分开报，用户才知道该重试还是该换链接
        raise HTTPException(
            status_code=502,
            detail={"error": "pob_fetch_failed", "reason": f"抓取分享链接失败：{exc}"},
        ) from exc
    except PobParseError as exc:
        # 422 而不是 400：格式对不上属于语义无法处理，且要把可读原因带回去
        raise HTTPException(
            status_code=422,
            detail={"error": "pob_parse_failed", "reason": str(exc)},
        ) from exc

    return _build_response(
        build,
        input_type=input_type,
        caller=caller,
        tree_link=tree_link,
        character_meta=character_meta,
        boss_query=req.boss_survival,
        boss_sequence_query=req.boss_sequence,
        boss_timed_sequence_query=req.boss_timed_sequence,
        boss_probabilistic_sequence_query=req.boss_probabilistic_sequence,
        boss_layered_sequence_query=req.boss_layered_sequence,
        boss_risk_audit_query=req.boss_risk_audit,
        growth_plan_query=req.growth_plan,
    )


@router.post("/build/compare", response_model=BuildCompareResponse)
def compare_builds(
    req: BuildCompareRequest,
    caller: str = Depends(require_user),
) -> BuildCompareResponse:
    """Compare multiple full PoB snapshots without recalculating or ranking them."""
    parsed_scenarios = []
    for scenario in req.scenarios:
        try:
            if looks_like_passive_tree_link(scenario.code):
                raise PobParseError(
                    "官方天赋树链接不含 PlayerStat，不能用于方案数值比较"
                )
            code = _resolve_code(scenario.code)
            build = bridge_gems(dbmod.get_conn(), parse_build(decode_share_code(code)))
        except (PobLinkError, Poe2SourceRejected) as exc:
            raise HTTPException(status_code=422, detail={
                "error": "build_compare_link_rejected",
                "scenario": scenario.label,
                "reason": str(exc),
            }) from exc
        except OSError as exc:
            raise HTTPException(status_code=502, detail={
                "error": "build_compare_fetch_failed",
                "scenario": scenario.label,
                "reason": f"抓取分享链接失败：{exc}",
            }) from exc
        except PobParseError as exc:
            raise HTTPException(status_code=422, detail={
                "error": "build_compare_parse_failed",
                "scenario": scenario.label,
                "reason": str(exc),
            }) from exc
        parsed_scenarios.append((scenario.label, build))

    try:
        comparison: BuildScenarioComparison = compare_build_scenarios(
            parsed_scenarios,
            metrics=list(req.metrics) if req.metrics is not None else None,
            reference_label=req.reference_label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_build_compare_query",
            "reason": str(exc),
        }) from exc
    logger.info(
        "Build 方案比较 caller=%s 方案=%d 基线=%s 指标=%d/%d/%d",
        caller,
        comparison.scenario_count,
        comparison.reference_label,
        comparison.ready_metrics,
        comparison.partial_metrics,
        comparison.unknown_metrics,
    )
    return BuildCompareResponse.model_validate(comparison, from_attributes=True)


@router.post("/build/screenshot", response_model=BuildResponse)
def analyze_screenshot_build(
    req: ScreenshotBuildRequest,
    caller: str = Depends(require_user),
) -> BuildResponse:
    """分析用户已确认的截图字段；没有确认标记时 Pydantic 会直接拒绝。"""
    fields = ScreenshotBuildFields(
        level=req.level,
        class_name=req.class_name.strip(),
        ascend_class_name=req.ascend_class_name.strip(),
        main_skill=req.main_skill.strip(),
        player_stats=dict(req.player_stats),
        player_stat_text={key: value.strip() for key, value in req.player_stat_text.items()},
        enemy_is_boss=req.enemy_is_boss,
    )
    build = screenshot_build(fields)
    logger.info(
        "截图 Build 已确认 caller=%s 字段=%d 文本字段=%d",
        caller, len(fields.player_stats), len(fields.player_stat_text),
    )
    return _build_response(
        build,
        input_type="screenshot",
        caller=caller,
        growth_plan_query=req.growth_plan,
    )


def _build_response(
    build,
    *,
    input_type: str,
    caller: str,
    tree_link=None,
    character_meta=None,
    boss_query: BuildBossSurvivalRequest | None = None,
    boss_sequence_query: BuildBossSequenceRequest | None = None,
    boss_timed_sequence_query: BuildBossTimedSequenceRequest | None = None,
    boss_probabilistic_sequence_query: BuildBossProbabilisticSequenceRequest | None = None,
    boss_layered_sequence_query: BuildBossLayeredSequenceRequest | None = None,
    boss_risk_audit_query: BuildBossRiskAuditRequest | None = None,
    growth_plan_query: BuildGrowthPlanRequest | None = None,
) -> BuildResponse:  # noqa: ANN001
    """四类 Build 输入共用同一份确定性报告装配，避免入口间口径漂移。"""
    report = build_health_report(dbmod.get_conn(), build)
    sources = build_defence_sources(build)
    suggestions = build_suggestions(build)
    version = check_build_version(dbmod.get_conn(), build)
    boss_result = None
    boss_sequence_result = None
    boss_timed_sequence_result = None
    boss_probabilistic_sequence_result = None
    boss_layered_sequence_result = None
    boss_risk_audit_result = None
    growth_plan_result: BuildGrowthPlan | None = None
    if boss_query is not None:
        checked = boss_survival_from_build(
            build,
            skill=boss_query.skill,
            level=boss_query.level,
            uber=boss_query.uber,
            roll_range=boss_query.roll_range,
            hit_count=boss_query.hit_count,
            use_pob_max_hit=boss_query.use_pob_max_hit,
            pool_mode=boss_query.pool_mode,
        )
        if not checked.ok:
            raise HTTPException(status_code=422, detail={
                "error": "invalid_build_boss_survival_query", "reason": checked.error,
            })
        boss_result = BuildBossSurvivalOut(
            **(checked.data or {}), citations=checked.citations
        )
    if boss_sequence_query is not None:
        checked_sequence = boss_sequence_from_build(
            build,
            sequence=[item.model_dump() for item in boss_sequence_query.sequence],
            level=boss_sequence_query.level,
            uber=boss_sequence_query.uber,
            roll_range=boss_sequence_query.roll_range,
            pool_mode=boss_sequence_query.pool_mode,
        )
        if not checked_sequence.ok:
            raise HTTPException(status_code=422, detail={
                "error": "invalid_build_boss_sequence_query",
                "reason": checked_sequence.error,
            })
        boss_sequence_result = BuildBossSequenceOut(
            **(checked_sequence.data or {}), citations=checked_sequence.citations
        )
    if boss_timed_sequence_query is not None:
        checked_timeline = boss_timed_sequence_from_build(
            build,
            events=[item.model_dump() for item in boss_timed_sequence_query.events],
            level=boss_timed_sequence_query.level,
            uber=boss_timed_sequence_query.uber,
            roll_range=boss_timed_sequence_query.roll_range,
            pool_mode=boss_timed_sequence_query.pool_mode,
            recovery_per_second=boss_timed_sequence_query.recovery_per_second,
            recovery_delay_seconds=boss_timed_sequence_query.recovery_delay_seconds,
        )
        if not checked_timeline.ok:
            raise HTTPException(status_code=422, detail={
                "error": "invalid_build_boss_timed_sequence_query",
                "reason": checked_timeline.error,
            })
        boss_timed_sequence_result = BuildBossTimedSequenceOut(
            **(checked_timeline.data or {}), citations=checked_timeline.citations
        )
    if boss_probabilistic_sequence_query is not None:
        probability_query = boss_probabilistic_sequence_query
        checked_probability = boss_probabilistic_sequence_from_build(
            build,
            events=[item.model_dump() for item in probability_query.events],
            level=probability_query.level,
            uber=probability_query.uber,
            roll_range=probability_query.roll_range,
            pool_mode=probability_query.pool_mode,
            recovery_per_second=probability_query.recovery_per_second,
            recovery_delay_seconds=probability_query.recovery_delay_seconds,
            attack_hit_chance_pct=probability_query.attack_hit_chance_pct,
            attack_block_chance_pct=probability_query.attack_block_chance_pct,
            spell_block_chance_pct=probability_query.spell_block_chance_pct,
            spell_suppression_chance_pct=(
                probability_query.spell_suppression_chance_pct
            ),
            blocked_damage_taken_pct=probability_query.blocked_damage_taken_pct,
            suppressed_damage_taken_pct=(
                probability_query.suppressed_damage_taken_pct
            ),
            bucket_size=probability_query.bucket_size,
        )
        if not checked_probability.ok:
            raise HTTPException(status_code=422, detail={
                "error": "invalid_build_boss_probabilistic_sequence_query",
                "reason": checked_probability.error,
            })
        boss_probabilistic_sequence_result = BuildBossProbabilisticSequenceOut(
            **(checked_probability.data or {}),
            citations=checked_probability.citations,
        )
    if boss_layered_sequence_query is not None:
        layered_query = boss_layered_sequence_query
        checked_layered = boss_layered_sequence_from_build(
            build,
            events=[item.model_dump() for item in layered_query.events],
            level=layered_query.level,
            uber=layered_query.uber,
            roll_range=layered_query.roll_range,
            mana_before_life_pct=layered_query.mana_before_life_pct,
            life_recovery_per_second=layered_query.life_recovery_per_second,
            energy_shield_recovery_per_second=(
                layered_query.energy_shield_recovery_per_second
            ),
            mana_recovery_per_second=layered_query.mana_recovery_per_second,
            energy_shield_recharge_per_second=(
                layered_query.energy_shield_recharge_per_second
            ),
            energy_shield_recharge_delay_seconds=(
                layered_query.energy_shield_recharge_delay_seconds
            ),
            energy_shield_recharge_interrupt_mode=(
                layered_query.energy_shield_recharge_interrupt_mode
            ),
        )
        if not checked_layered.ok:
            raise HTTPException(status_code=422, detail={
                "error": "invalid_build_boss_layered_sequence_query",
                "reason": checked_layered.error,
            })
        boss_layered_sequence_result = BuildBossLayeredSequenceOut(
            **(checked_layered.data or {}), citations=checked_layered.citations,
        )
    if boss_risk_audit_query is not None:
        checked_audit = boss_risk_audit_from_build(
            build,
            level=boss_risk_audit_query.level,
            hit_count=boss_risk_audit_query.hit_count,
            uber=boss_risk_audit_query.uber,
            roll_range=boss_risk_audit_query.roll_range,
            pool_mode=boss_risk_audit_query.pool_mode,
        )
        if not checked_audit.ok:
            raise HTTPException(status_code=422, detail={
                "error": "invalid_build_boss_risk_audit_query",
                "reason": checked_audit.error,
            })
        boss_risk_audit_result = BuildBossRiskAuditOut(
            **(checked_audit.data or {}), citations=checked_audit.citations
        )
    if growth_plan_query is not None:
        objectives = [
            GrowthObjective(
                metric=item.metric,
                target=item.target,
                priority=item.priority,
                label=item.label,
            )
            for item in growth_plan_query.objectives
        ]
        growth_plan_result = plan_build_growth(build, objectives)
    bridged = sum(1 for g in build.skill_groups for x in g.gems if x.gem_id)
    total_gems = sum(len(g.gems) for g in build.skill_groups)
    logger.info(
        "build 解析 caller=%s 等级=%s 职业=%s/%s 技能组=%d 宝石=%d/%d 装备=%d",
        caller, build.level, build.class_name, build.ascend_class_name,
        len(build.skill_groups), bridged, total_gems, len(build.items),
    )

    return BuildResponse(
        input_type=input_type,
        level=build.level,
        class_name=build.class_name,
        ascend_class_name=build.ascend_class_name,
        bandit=build.bandit,
        pantheon_major=build.pantheon_major,
        pantheon_minor=build.pantheon_minor,
        skill_groups=[
            SkillGroupOut(
                slot=g.slot, enabled=g.enabled, is_main=g.is_main,
                gems=[
                    GemOut(
                        name_en=x.name_en, name_zh=x.name_zh, level=x.level,
                        quality=x.quality, enabled=x.enabled, gem_id=x.gem_id,
                    )
                    for x in g.gems
                ],
            )
            for g in build.skill_groups
        ],
        items=[
            ItemOut(
                slot=i.slot,
                name=i.parsed.name if i.parsed else "",
                base_type=i.parsed.base_type if i.parsed else "",
                rarity=i.parsed.rarity.value if i.parsed else "",
                item_level=i.parsed.item_level if i.parsed else None,
                implicits=[m.text for m in i.parsed.implicits] if i.parsed else [],
                explicits=[m.text for m in i.parsed.explicits] if i.parsed else [],
                error=i.error,
            )
            for i in build.items
        ],
        config={k: str(v) for k, v in build.config.items()},
        player_stats=build.player_stats,
        player_stats_note=(
            _SCREENSHOT_STATS_NOTE if input_type == "screenshot" and build.player_stats
            else _STATS_NOTE if build.player_stats else ""
        ),
        boss_survival=boss_result,
        boss_sequence=boss_sequence_result,
        boss_timed_sequence=boss_timed_sequence_result,
        boss_probabilistic_sequence=boss_probabilistic_sequence_result,
        boss_layered_sequence=boss_layered_sequence_result,
        boss_risk_audit=boss_risk_audit_result,
        growth_plan=(
            BuildGrowthPlanOut.model_validate(growth_plan_result, from_attributes=True)
            if growth_plan_result else None
        ),
        report=[_check_out(c) for c in report.items],
        output_report=[_check_out(c) for c in report.output_items],
        defence_sources=[DefenceSourceOut(**line.__dict__) for line in sources.lines],
        defence_sources_unresolved=sources.unresolved,
        defence_sources_note=sources.note,
        suggestions=[BuildSuggestionOut(**item.__dict__) for item in suggestions],
        suggestions_note=(
            "建议只由已知 PlayerStat 短板触发；预期收益是当前值到目标值的可复核差额。"
            "标准条目价格快照已接入，但缺完整制作路径与材料清单；"
            "成本按改造规模分档，不提供虚构的方案通货报价。"
        ),
        version_check=BuildVersionOut(**version.__dict__),
        tree_serialization_version=(tree_link.serialization_version if tree_link else None),
        ascendancy_id=(tree_link.ascendancy_id if tree_link else None),
        secondary_ascendancy_id=(tree_link.secondary_ascendancy_id if tree_link else None),
        mastery_effects=(
            dict(tree_link.mastery_effects) if tree_link else
            dict(character_meta.mastery_effects) if character_meta else {}
        ),
        character_name=character_meta.name if character_meta else "",
        character_league=character_meta.league if character_meta else "",
        character_realm=character_meta.realm if character_meta else "",
        alternate_ascendancy_name=(
            character_meta.alternate_ascendancy if character_meta else ""
        ),
        report_notes=report.notes,
        analysis_done=True,
        analysis_note=(
            _CHARACTER_ANALYSIS_NOTE if input_type == "character_api"
            else _SCREENSHOT_ANALYSIS_NOTE if input_type == "screenshot"
            else _ANALYSIS_NOTE
        ),
        warnings=build.warnings,
    )
