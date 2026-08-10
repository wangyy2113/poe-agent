"""天赋节点 → 知识卡片。

`kb/render.py` 的 `render_card` 写死了「类型：唯一装备」与 `Category.ITEM`，
不能复用，所以单独一支。两处刻意的差别：

1. **大师天赋（Mastery）必须标出「选一」。** 一个大师节点列六个效果，点它时
   只能选其中一个。若把六条平铺成列表，模型会说这个节点给了六条属性——那是
   一个听起来完全合理的错答案。所以「选一」既在 `ModKind.MASTERY` 上（结构层），
   也在卡片小标题上（文本层）。
2. **含换行的属性用续行缩进，不用 ` / ` 拼接。** 装备词缀那边把 POEDB 的 `<br>`
   规范成 ` / `（见 1b.4），因为那里的换行确定是两条属性的分隔。天赋这边不然：
   同样的 `\\n` 有时是两条独立属性，有时只是一句话折行（`每因此失去 10 点怒火，
   \\n获得 1 秒狂野兽性`）。分不清就不改写原文。
"""

from __future__ import annotations

import hashlib

from entities.models import Chunk, DocumentMetadata, Entity
from entities.taxonomy import Category, EntityType, ModKind, SubCategory
from retrieval.tokenize import content_words, tokens_for_fts

#: 天赋卡都很短（实测最长几百字），不需要切分。留个上限做保险。
MAX_CHUNK_CHARS = 1600

_SUBCATEGORY = {
    EntityType.PASSIVE: SubCategory.PASSIVE.value,
    EntityType.ASCENDANCY: SubCategory.ASCENDANCY.value,
    EntityType.ATLAS_PASSIVE: SubCategory.ATLAS_PASSIVE.value,
}
_CATEGORY = {
    EntityType.PASSIVE: Category.BASICS,
    EntityType.ASCENDANCY: Category.BASICS,
    EntityType.ATLAS_PASSIVE: Category.ATLAS,
}


def _stat_lines(texts: list[str]) -> list[str]:
    """一条属性一个列表项。含换行的用续行缩进，保持原文不动。"""
    out: list[str] = []
    for t in texts:
        parts = [p for p in t.split("\n") if p.strip()]
        if not parts:
            continue
        out.append(f"- {parts[0]}")
        # Markdown 的懒惰续行：缩进两格即视为同一列表项，原文一字未改
        out.extend(f"  {p}" for p in parts[1:])
    return out


def render_card(entity: Entity) -> str:
    """渲染天赋卡。

    ## 卡片里不放通用脚手架词

    充分依据的判据是**查询实义词在卡片正文里的覆盖率**（`retrieval/fts.term_coverage`
    用的就是 `chunk.text`），所以每张卡里的每个词都在给「这张卡与该问题相关」投票。

    实测代价：初版给卡片加了 `**属性**` 与 `**机制说明**` 两个小标题、外加一句
    「在页面搜索框输入 X 可定位该节点」的导航说明，结果 2,470 张卡**每张**都含
    属性 / 机制 / 页面 / 搜索 / 节点 这些高频提问词。于是「玄虚之靴有什么属性」
    （POEDB 无此名，必须拒答）靠 `之靴` + `属性` 拿到 67% 覆盖率通过了闸门。
    这是无据漏出，本项目最严重的一类错误。

    规则：**只写能标识这张卡主题的词，不写放在任何卡上都成立的词。**
    「天赋树」「基石」「升华」留着——它们确实是这张卡的主题；
    「属性」「机制」「所在」「类型」去掉——每张卡都能这么说，等于没说。
    导航说明属于展示层，移到 `retrieval` 之外（工具层可以讲怎么定位节点），
    不进可引用证据。
    """
    attrs = entity.attributes
    kind_zh = attrs.get("node_kind_zh") or "天赋"
    head = entity.name_zh
    if entity.name_en:
        head = f"{entity.name_zh}（{entity.name_en}）"

    tree_zh = "Atlas 天赋树" if attrs.get("tree") == "atlas-skill-tree" else "天赋树"
    parts: list[str] = [f"# {kind_zh}·{head}"]

    # 一行元信息，只放值不放标签名
    meta: list[str] = [kind_zh]
    if attrs.get("ascendancy_zh"):
        meta.append(f"{attrs.get('class_zh') or ''} · {attrs['ascendancy_zh']}".strip(" ·"))
    meta.append(f"{tree_zh} {attrs.get('tree_version') or ''}".strip())
    positions = int(attrs.get("positions") or 1)
    if positions > 1:
        # 「树上 N 处」是有用的事实：同名同效的节点可以分别点取，效果叠加
        meta.append(f"树上 {positions} 处（同名同效，可分别点取）")
    parts.append(" ｜ ".join(meta))

    stats = [m.text for m in entity.mods if m.kind is ModKind.PASSIVE]
    if stats:
        parts.append("")
        parts.extend(_stat_lines(stats))

    effects = attrs.get("mastery_effects") or []
    if effects:
        parts.append("")
        # 这段留着：它只出现在 61 张大师天赋卡上（不是全量），而「只能选一个」
        # 是不说就会答错的事实——平铺六条会让人以为六条同时生效
        parts.append(f"以下 {len(effects)} 个效果**只能选定其中一个**，不是同时获得：")
        for i, eff in enumerate(effects, 1):
            lines = _stat_lines(eff.get("stats") or [])
            if not lines:
                continue
            parts.append(f"{i}. {lines[0][2:]}")
            parts.extend(f"   {ln}" for ln in lines[1:])

    reminder = [m.text for m in entity.mods if m.kind is ModKind.DESCRIPTION]
    if reminder:
        parts.append("")
        # 游戏自带的括号说明，本身读起来就是注解，不需要再加小标题
        parts.extend(f"> {p}" for t in reminder for p in t.split("\n") if p.strip())

    parts.append("")
    parts.append(f"来源：{entity.ref.source} → {tree_zh} · {head}")
    parts.append(entity.ref.url)
    return "\n".join(parts)


def build_metadata(entity: Entity) -> DocumentMetadata:
    attrs = entity.attributes
    title = entity.name_zh
    if entity.name_en:
        title = f"{entity.name_zh}（{entity.name_en}）"
    kind_zh = attrs.get("node_kind_zh") or "天赋"
    # 标题带种类：检索结果列表里「基石·神威之盾」比裸名字有信息量，
    # 也避免与同名的唯一装备混淆（实测 29 个天赋名与唯一装备重名）
    title = f"{kind_zh}·{title}"

    tags = [kind_zh]
    if attrs.get("ascendancy_zh"):
        tags += [attrs["ascendancy_zh"], attrs.get("class_zh") or ""]
    return DocumentMetadata(
        title=title,
        category=_CATEGORY.get(entity.entity_type, Category.BASICS),
        subcategory=_SUBCATEGORY.get(entity.entity_type, SubCategory.PASSIVE.value),
        source=entity.ref.source,
        source_priority=entity.ref.priority,
        url=entity.ref.url,
        version=entity.version,
        language="zh-CN",
        tags=[t for t in tags if t][:8],
        facets={
            "node_kind": str(attrs.get("node_kind") or ""),
            "tree": str(attrs.get("tree") or ""),
            "ascendancy": str(attrs.get("ascendancy_zh") or ""),
            "positions": str(attrs.get("positions") or 1),
            # 大师天赋的效果是互斥的，检索侧也应该能看出来而不是只在正文里
            "mutually_exclusive": "true" if attrs.get("mastery_effects") else "false",
        },
        fetched_at=entity.ref.fetched_at,
    )


def to_chunks(entity: Entity) -> list[Chunk]:
    if not entity.indexable:
        return []
    text = render_card(entity)
    if len(text) > MAX_CHUNK_CHARS:
        text = text[:MAX_CHUNK_CHARS]
    metadata = build_metadata(entity)
    keywords = " ".join(
        content_words(
            [m.text for m in entity.mods if m.kind is not ModKind.DESCRIPTION],
            extra=entity.search_keywords,
        )
    )
    digest = hashlib.sha256(entity.entity_id.encode()).hexdigest()[:16]
    return [
        Chunk(
            chunk_id=f"{entity.entity_id}#{digest}",
            entity_id=entity.entity_id,
            text=text,
            tokens=tokens_for_fts(text),
            keywords=keywords,
            metadata=metadata,
        )
    ]
