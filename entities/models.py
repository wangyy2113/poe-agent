"""实体与文档模型（Tier A 精确层 / Tier B 语义层）。

设计要点见 design.md 5.1：
  数值走结构化通道（Entity.mods），语义走检索通道（Chunk）；
  两层用 entity_id 互指，检索命中后可无损取回准确数值。

诚实性约束在模型层就固化：
  - 缺 url 或 source 的文档不可引用（indexable=False）
  - name_en 允许为 None，**不得由 slug 反推**（Abberaths_Horn → Abberath's Horn，撇号会丢）
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

from entities.taxonomy import (
    MAX_FACTUAL_PRIORITY,
    Category,
    EntityType,
    ModKind,
)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ValueRange(BaseModel):
    """词缀数值范围。POEDB 用 em dash 表示区间，如 `+(40—55) 力量`。"""

    low: float
    high: float

    @property
    def is_fixed(self) -> bool:
        return self.low == self.high

    def render(self) -> str:
        if self.is_fixed:
            return _fmt(self.low)
        return f"({_fmt(self.low)}-{_fmt(self.high)})"


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


class ModLine(BaseModel):
    """一条词缀。

    `text` 保留 POEDB 原文（含区间标记），不做改写——数值必须可回溯到原文。
    `ranges` 是从原文解析出的结构化区间，供计算与 T 级匹配使用。
    """

    kind: ModKind
    text: str
    ranges: list[ValueRange] = Field(default_factory=list)

    @property
    def is_factual(self) -> bool:
        """括号说明文本不是事实词条，不参与词缀统计。"""
        return self.kind is not ModKind.DESCRIPTION


class Requirement(BaseModel):
    level: int | None = None
    strength: int | None = None
    dexterity: int | None = None
    intelligence: int | None = None
    raw: str = ""


class SourceRef(BaseModel):
    source: str
    url: str
    priority: int
    fetched_at: str = Field(default_factory=utcnow_iso)

    @property
    def can_support_facts(self) -> bool:
        """P6 社区来源不得支撑事实断言。"""
        return self.priority <= MAX_FACTUAL_PRIORITY


class Entity(BaseModel):
    """Tier A：结构化实体。"""

    entity_id: str
    entity_type: EntityType
    name_zh: str
    #: 权威英文名。未从详情页取到时为 None——**禁止由 slug 反推**。
    name_en: str | None = None
    slug: str = ""
    base_type: str | None = None
    item_class: str | None = None

    mods: list[ModLine] = Field(default_factory=list)
    requirement: Requirement | None = None
    flavour_text: str | None = None
    #: 补偿向量通道关闭时的描述性查询能力，写入 FTS5 的 keywords 列
    search_keywords: list[str] = Field(default_factory=list)
    attributes: dict = Field(default_factory=dict)

    ref: SourceRef
    league: str | None = None
    version: str | None = None
    content_hash: str = ""

    @field_validator("name_zh")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("name_zh 不得为空")
        return v.strip()

    @property
    def indexable(self) -> bool:
        """缺 url 或 source 的实体不得进入可引用索引（需求 2.5）。"""
        return bool(self.ref.url.strip()) and bool(self.ref.source.strip())

    @property
    def factual_mods(self) -> list[ModLine]:
        return [m for m in self.mods if m.is_factual]

    def mods_of(self, kind: ModKind) -> list[ModLine]:
        return [m for m in self.mods if m.kind is kind]


class DocumentMetadata(BaseModel):
    """Tier B 切片的元数据（需求 2.4 要求的完整字段）。"""

    title: str
    category: Category
    subcategory: str = ""
    source: str
    source_priority: int
    url: str
    league: str | None = None
    version: str | None = None
    language: str = "zh-CN"
    tags: list[str] = Field(default_factory=list)
    #: 领域维度，供检索侧做偏好与过滤（如词缀来源 mod_source、底材类别 item_class）。
    #: 放在结构化字段里而不是让检索去解析标题——标题格式一变，排序就会静默失准。
    facets: dict[str, str] = Field(default_factory=dict)
    fetched_at: str = Field(default_factory=utcnow_iso)

    @property
    def indexable(self) -> bool:
        return bool(self.url.strip()) and bool(self.source.strip())

    @property
    def can_support_facts(self) -> bool:
        return self.source_priority <= MAX_FACTUAL_PRIORITY


class Chunk(BaseModel):
    """Tier B：知识卡片切片。`entity_id` 回指 Tier A 以取回精确数值。"""

    chunk_id: str
    entity_id: str | None
    text: str
    #: jieba 预分词后的空格分隔词序列，写入 FTS5 的 tokens 列
    tokens: str = ""
    #: 描述性关键词，写入 FTS5 的 keywords 列
    keywords: str = ""
    metadata: DocumentMetadata


class Citation(BaseModel):
    """引用。渲染为「来源：POEDB → 猎首（Headhunter）」+ 可点击 URL。"""

    source: str
    title: str
    url: str
    entity_id: str | None = None
    priority: int = 99
    fetched_at: str = Field(default_factory=utcnow_iso)

    def render(self) -> str:
        return f"来源：{self.source} → {self.title}"

    @property
    def can_support_facts(self) -> bool:
        return self.priority <= MAX_FACTUAL_PRIORITY


def entity_to_citation(entity: Entity) -> Citation:
    """实体 → 引用。英文名存在时并列展示，便于对照社区资料。"""
    title = entity.name_zh
    if entity.name_en:
        title = f"{entity.name_zh}（{entity.name_en}）"
    return Citation(
        source=entity.ref.source,
        title=title,
        url=entity.ref.url,
        entity_id=entity.entity_id,
        priority=entity.ref.priority,
        fetched_at=entity.ref.fetched_at,
    )
