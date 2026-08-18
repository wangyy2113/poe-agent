"""领域词表：从知识库数据生成，供查询扫描使用。

## 为什么需要它

通用中文分词不认识 POE 的领域实体边界。实测「弓上的火焰伤害前缀有哪些等级」被
jieba 切成 `['弓上', '的', '火焰', ...]`——底材类别「弓」被和后接的「上」粘成
`弓上`，而知识库里写的是单字「弓」。结果是**物品类别这一维在检索里完全丢失**：
查询问弓，返回的却是爪、匕首、短杖的火焰词缀。

这类错误正是「无据不答」要防的：答案看起来像回答了问题，实际说的是另一个物品类别。

## 为什么不用 jieba 自定义词典

实测 `jieba.add_word("弓", freq=100000)` 对 `弓上` 无效——`弓上` 已在 jieba 词典中
且路径概率占优。改用 `del_word` / `suggest_freq` 则需要逐一枚举「类别名 + 后接字」
的粘连组合（弓上、爪上、盾上……），POEDB 每加一个类别就要补一次，是脆弱方案。

## 做法

在**原始查询串**上直接扫描已知领域词，与 jieba 结果取并集。词表全部由数据导出，
不含任何凭记忆写下的术语：

| 类别 kind      | 来源列                      | 说明                          |
| ------------- | -------------------------- | ----------------------------- |
| `item_class`  | `mod_affix.item_class_zh`  | 底材类别，74 个，含单字「弓」「爪」 |
| `mod_source`  | `mod_affix.category_label` | 词缀来源：精华 / 工艺工作台 / 地心探索 … |
| `gen`         | `mod_affix.gen_label`      | 前缀 / 后缀                     |
| `unique`      | `entity.name_zh`           | 唯一装备中文名                   |
| `base_type`   | `entity.base_type`         | 底材名                         |
| `broad_class` | `entity.item_class`        | 武器 / 护甲 / 其他               |

词表落盘到 `data/dict/poe_terms.txt`（`词条<TAB>kind`），索引构建与查询读同一份，
保证两侧一致。文件不存在时退化为纯 jieba 行为，不影响启动。

kind 不只是注释：检索侧要判断「查询有没有指定词缀来源」，据此决定是偏向被点名的
来源还是偏向常规掉落池（见 `retrieval/hybrid.py`）。这个判断必须基于数据里真实
存在的来源标签，不能靠手写清单。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

#: 词表相对工作目录的位置。与 data/ 下其他派生产物同级，已 gitignore。
VOCAB_PATH = Path("data") / "dict" / "poe_terms.txt"

#: 单字词只有在词表里确实存在时才当作领域词。这里不设白名单，
#: 完全以数据为准——POEDB 里 item_class_zh 有「弓」「爪」就收，没有就不收。
_MIN_LEN = 1

#: kind → 取词 SQL。顺序即优先级：同一词条被多处收录时保留先出现的 kind。
_SOURCES: tuple[tuple[str, str], ...] = (
    ("item_class", "SELECT DISTINCT item_class_zh FROM mod_affix WHERE item_class_zh <> ''"),
    ("mod_source", "SELECT DISTINCT category_label FROM mod_affix WHERE category_label <> ''"),
    ("gen", "SELECT DISTINCT gen_label FROM mod_affix WHERE gen_label <> ''"),
    ("unique", "SELECT DISTINCT name_zh FROM entity WHERE name_zh IS NOT NULL AND name_zh <> ''"),
    ("base_type", "SELECT DISTINCT base_type FROM entity WHERE base_type IS NOT NULL AND base_type <> ''"),
    ("broad_class", "SELECT DISTINCT item_class FROM entity WHERE item_class IS NOT NULL AND item_class <> ''"),
)

_lock = threading.Lock()
#: (按长度降序的词条列表, 词条 → kind)
_cache: tuple[list[str], dict[str, str]] | None = None


def _sql_terms(conn: sqlite3.Connection) -> dict[str, str]:
    """从库里取出全部领域词及其 kind。只读已入库的事实，不做任何推断。"""
    kinds: dict[str, str] = {}
    for kind, sql in _SOURCES:
        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.OperationalError as exc:
            # 表还没建（首次只跑某一类）不是错误，跳过即可
            logger.debug("词表来源不可用，跳过 %s：%s（%s）", kind, sql, exc)
            continue
        for r in rows:
            term = (r[0] or "").strip()
            # 只收纯词，含空格或换行的是渲染文本不是术语
            if len(term) >= _MIN_LEN and not any(c.isspace() for c in term):
                kinds.setdefault(term, kind)
    return kinds


def _sorted_terms(kinds: dict[str, str]) -> list[str]:
    return sorted(kinds, key=lambda t: (-len(t), t))


def build_vocab(conn: sqlite3.Connection, path: Path | None = None) -> int:
    """从库里导出词表并落盘，同时刷新进程内缓存。

    返回词条数。管线在**渲染卡片之前**调用，使索引与查询用同一份词表。
    """
    global _cache
    target = path or VOCAB_PATH
    kinds = _sql_terms(conn)
    terms = _sorted_terms(kinds)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 显式 encoding：Windows 默认 cp936 会破坏中文（项目跨平台硬规则）
    with target.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("# 由 kb.pipeline 从知识库导出，请勿手工编辑\n")
        fh.write("# 格式：词条<TAB>kind\n")
        for t in terms:
            fh.write(f"{t}\t{kinds[t]}\n")
    with _lock:
        _cache = (terms, kinds)
    by_kind: dict[str, int] = {}
    for k in kinds.values():
        by_kind[k] = by_kind.get(k, 0) + 1
    logger.info("领域词表已生成：%d 条 %s → %s", len(terms), by_kind, target)
    return len(terms)


def _load(path: Path | None = None) -> tuple[list[str], dict[str, str]]:
    global _cache
    if _cache is not None:
        return _cache
    with _lock:
        if _cache is not None:
            return _cache
        target = path or VOCAB_PATH
        kinds: dict[str, str] = {}
        if target.exists():
            with target.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.rstrip("\n")
                    if not line or line.startswith("#"):
                        continue
                    term, _, kind = line.partition("\t")
                    term = term.strip()
                    if term:
                        kinds.setdefault(term, kind.strip() or "unknown")
        else:
            logger.debug("领域词表不存在（%s），退化为纯分词", target)
        _cache = (_sorted_terms(kinds), kinds)
        return _cache


def load_vocab(path: Path | None = None) -> list[str]:
    """读取词表（按长度降序）。文件不存在时返回空表，调用方退化为纯 jieba。"""
    return _load(path)[0]


def terms_of_kind(kind: str) -> set[str]:
    """某一 kind 的全部词条。"""
    return {t for t, k in _load()[1].items() if k == kind}


def reset_cache() -> None:
    """丢弃进程内缓存，下次访问重新读盘。测试与管线换库时用。"""
    global _cache
    with _lock:
        _cache = None


def scan_terms(text: str) -> list[str]:
    """在原串上扫出领域词，只保留**极大匹配**。

    极大匹配的意思：若「召集法杖」命中，就不再单独返回「法杖」——
    那是另一个物品类别，混进来会让覆盖率虚高并干扰排序。
    """
    if not text:
        return []
    vocab = load_vocab()
    if not vocab:
        return []

    found = [t for t in vocab if t in text]  # vocab 已按长度降序
    out: list[str] = []
    for t in found:
        # 已收录的更长词若包含 t，说明 t 是它的一部分，丢弃
        if any(t != longer and t in longer for longer in out):
            continue
        out.append(t)
    return out
