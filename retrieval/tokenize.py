"""中文分词。抽取器与 FTS5 索引共用同一套切分，保证写入与查询一致。

为什么要预分词（需求 3.4）：
    SQLite FTS5 的 unicode61 分词器不认识中文词边界，直接喂中文会退化成
    按字符切分，检索质量很差。因此在**写入前**用 jieba 切成空格分隔的词序列
    存进 tokens 列，查询时对查询串做同样处理，unicode61 按空格切分即可正确工作。
    这样不需要编译自定义分词扩展，跨平台无额外依赖。
"""

from __future__ import annotations

import logging
import re
import threading

logger = logging.getLogger(__name__)

_init_lock = threading.Lock()
_initialized = False

#: 功能词：分词后单独出现时不构成检索线索。
#: 只过滤整词，**不在词内部删字符**——那样会把「暴击导致的点燃持续伤害加成」
#: 削成「暴击导致燃持续伤害成」，破坏术语。
#:
#: 第二段是疑问词与寒暄词。它们不出现在任何知识卡里，留着只会让覆盖率被永久
#: 压低、并在 missing_terms 里冒充「知识库缺这个」。**只收纯疑问 / 寒暄词**：
#: 像「需要」「最大」这类实义动词形容词一律保留——过度过滤会抬高覆盖率、
#: 放松充分依据闸门，导致无据漏出（需求 1.3 要求两个方向都防）。
STOPWORDS = frozenset(
    """
    的 了 和 与 或 在 对 为 是 被 使 你 我 它 每 个 所 有 当 若 则 会 将 不 无 该 此 上 下 内 外
    以及 并且 因为 所以 如果 但是 而且 或者 可以 不能 已经 正在 时候 一名 一个 这个 那个
    哪 谁 吗 呢 吧 啊 哪些 哪个 哪里 什么 怎么 怎样 如何 多少 多久 为什么 是否 请问 告诉
    """.split()
)

#: 疑问数量构式：`几` / `多少` 加一个可选量词。
#:
#: 为什么用构式而不是往 STOPWORDS 里继续加词：这类组合是开放的
#: （几级 / 几档 / 几条 / 几个 / 几点 / 几种 / 多少条 …），枚举补不完，
#: 而漏一个的代价是实打实的——实测「爪的攻击速度后缀有几级」被 jieba 切出
#: `几级`，它在任何知识卡里都不存在，于是既永久压低覆盖率，又在 missing_terms
#: 里冒充「知识库缺几级」。这与 `orchestrator/binding.py` 把缺据声明的识别
#: 从词表改成构式匹配是同一个教训。
#:
#: 刻意只覆盖**纯疑问数量**：`几率` 不匹配（量词表里没有「率」），
#: 因为「几率」是实义词而且是词缀原文里的高频词。
_INTERROGATIVE_QTY_RE = re.compile(r"^(?:几|多少)(?:个|条|级|档|点|种|件|张|次|层|阶|颗|把|支|页)?$")

_PUNCT_RE = re.compile(r"[0-9%+\-()（）\[\]{},.:：、。；;“”\"'’!！?？…—–~/\\|*&^$#@]+")
_MEANINGFUL_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z]{2,}")


def is_stopword(token: str) -> bool:
    """整词停用判定：显式词表 ∪ 疑问数量构式。

    所有过滤点都走这一个函数，避免「查询侧过滤了、索引侧没过滤」这类分叉。
    """
    return token in STOPWORDS or bool(_INTERROGATIVE_QTY_RE.match(token))


def _ensure_jieba():
    """延迟初始化。jieba 初始化时会把自己的 logger 设成 DEBUG 并直接打印
    「Building prefix dict...」，这里显式压掉，避免刷屏。"""
    global _initialized
    import jieba  # noqa: PLC0415

    if _initialized:
        return jieba
    with _init_lock:
        if not _initialized:
            jieba.setLogLevel(logging.WARNING)
            logging.getLogger("jieba").setLevel(logging.WARNING)
            jieba.initialize()
            _initialized = True
    return jieba


def segment(text: str) -> list[str]:
    """切词，保留原词不做过滤。"""
    if not text:
        return []
    jieba = _ensure_jieba()
    return [t for t in (tok.strip() for tok in jieba.cut(text)) if t]


def tokens_for_fts(text: str) -> str:
    """转成空格分隔的词序列，供 FTS5 的 tokens 列使用。

    数字与百分号保留（`+45`、`20%` 本身是检索线索），
    但不做停用词过滤——tokens 列要尽量完整以支撑精确匹配。
    """
    if not text:
        return ""
    parts = [t for t in segment(text) if t and not t.isspace()]
    return " ".join(parts)


def content_words(texts: list[str], extra: list[str] | None = None) -> list[str]:
    """从多段文本抽实义词，供 FTS5 的 keywords 列使用（需求 3.12）。

    去数值与标点后分词，按**整词**过滤停用词，保证「点燃」「持续伤害」
    这类术语完整保留。去重且保序。
    """
    words: list[str] = list(extra or [])
    for text in texts:
        cleaned = _PUNCT_RE.sub(" ", text)
        for token in segment(cleaned):
            if len(token) < 2 or is_stopword(token):
                continue
            if not _MEANINGFUL_RE.search(token):
                continue
            words.append(token)

    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def _drop_glued(tokens: list[str], domain: list[str]) -> list[str]:
    """剔除被领域词粘连污染的分词结果。

    jieba 把「弓上的」切成 `弓上`+`的`，而语料里只有单字「弓」——`弓上` 在任何
    文档中都不存在。留着它会：① 永久压低覆盖率；② 在 missing_terms 里冒充
    「知识库缺弓上」，误导排查方向。
    """
    if not domain:
        return tokens
    dset = set(domain)
    return [t for t in tokens if t in dset or not any(d in t for d in domain)]


def query_content_words(query: str) -> list[str]:
    """查询侧实义词 = 领域词表扫描结果 ∪ jieba 实义词（去掉粘连词）。

    与 `content_words` 分开是为了性能：领域词扫描只在查询侧做（查询串短），
    索引侧走原路径，避免对 2.4 万张卡片各做上千次子串匹配。
    索引侧本来就把领域词按原文写进了 tokens 列，不需要再扫。
    """
    from retrieval import vocab  # noqa: PLC0415  延迟导入避免循环依赖

    domain = vocab.scan_terms(query)
    rest = _drop_glued(content_words([query]), domain)
    out = list(domain)
    for t in rest:
        if t not in out:
            out.append(t)
    return out


def query_tokens(query: str) -> str:
    """把用户查询转成 FTS5 MATCH 表达式。

    对每个词做转义并以 OR 连接：宁可召回多一些，排序阶段再收紧。

    领域词按**分词后的形式**写成短语。原因：索引侧 tokens 列存的是 jieba 切开、
    空格分隔的词序列，unicode61 按空格切分；直接写 `"召集法杖"` 会被当作单个
    token 而永远匹配不上，写成 `"召集 法杖"` 才是相邻短语匹配。
    """
    from retrieval import vocab  # noqa: PLC0415

    domain = vocab.scan_terms(query)
    parts = [t for t in segment(query) if len(t) >= 1 and not is_stopword(t) and not t.isspace()]
    parts = _drop_glued(parts, domain)

    exprs: list[str] = []
    for t in domain:
        phrase = " ".join(segment(t))
        exprs.append('"%s"' % phrase.replace('"', '""'))
    dset = set(domain)
    for t in parts:
        if t in dset:
            continue
        exprs.append('"%s"' % t.replace('"', '""'))
    if not exprs:
        return ""
    # FTS5 字符串里双引号需转义；用引号包裹可避免词本身被当作语法
    return " OR ".join(dict.fromkeys(exprs))
