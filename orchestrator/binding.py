"""断言—引用绑定校验：出口闸门（任务 3.6，需求 1.2/1.7/1.8/15.2/15.3/15.4）。

这是「无据不答」的最后一道防线，也是唯一由**代码**执行而非依赖模型自觉的一道。

做法：
  1. 进入生成的每条证据分配编号 `E1..En`，随内容一起进上下文。
  2. 提示词要求每个事实句末尾附 `[[E<n>]]`。
  3. 出口逐句校验：分句 → 分类 → 检查绑定 → 检查证据是否真正支撑该句。
  4. 未通过者**由代码拦截**并替换为该点的缺据声明。

为什么校验「证据是否真正支撑」而不只是「有没有标注」：模型完全可以随手写个
`[[E1]]` 蒙过去。所以对事实句里出现的**数值**做回填校验——数值必须能在它绑定的
证据原文里找到。这一条能抓住绝大多数编造，因为编造的具体来说就是数字。

三类内容分级（需求 15.2–15.4）：
  事实断言   强制绑定引用，未绑定即拦截
  计算派生   允许，但要有推导链且输入可溯源
  建议       允许，必须可辨识为建议
过渡句（「下面分几点说明」）不承载事实，放行。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

logger = logging.getLogger(__name__)

SentenceKind = Literal["factual", "derived", "recommendation", "narrative"]

#: 绑定标记。允许 `[[E1]]`、`[[E1,E3]]`、`[[E1][E3]]` 几种写法——
#: 模型的输出格式不会百分百听话，为格式差异拦掉有据的句子是「错误拒答」，
#: 与漏出同样要压。
_BINDING_RE = re.compile(r"\[\[\s*((?:E\d+\s*[,，;；]?\s*)+)\]\]", re.I)
_LABEL_RE = re.compile(r"E(\d+)", re.I)

#: 分句。中文标点 + 换行。**不切小数点**（「3.5 秒」不能被拆开），
#: 也不切绑定标记内部——`[[E1;E2]]` 里的分号是标记的一部分，
#: 切开会让标记失效、有据的句子被误拦（错误拒答）。
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*|\n+")

#: Markdown 结构行：标题、加粗标签、水平分隔线、空列表符号。
#: 这些是**排版**而非断言。早先按事实句处理，实测一次回答 16 句里拦了 5 句，
#: 全是 `## 猎首（Headhunter）`、`**隐式词缀**`、`---` 这类——答案结构被撕碎，
#: 属严重的错误拒答。
#: 但带数字的标题仍走事实句校验：数值是编造的主要载体，
#: `## 生命上限 1000` 这种要拦。
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", re.S)
_MD_RULE_RE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_MD_LABEL_RE = re.compile(r"^\s*(?:[-*+]\s+)?\*{1,3}([^*\n]{1,40})\*{1,3}\s*[：:]?\s*$")
_MD_BULLET_ONLY_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s*$")

#: 纯过渡/元话语句：只描述回答本身的组织，不承载游戏事实。
#: 这是**白名单**——判不出来的一律按事实句处理（见 `classify`）。
#: 锚定在句首并限制长度，避免「另外，猎首还会让你免疫冰冻」这类
#: 以连接词开头的真事实断言蒙混过关。
_NARRATIVE_RE = re.compile(
    r"^[\s，,、]*(?:下面|以下|接下来|下文|综上|综合以上|总结|小结|先说结论"
    r"|需要说明的?是|说明如下|分别说明|逐条说明|简要说明|回答如下)"
    r"[^。！？\n]{0,12}[。！？：:\s]*$"
)

#: 数值。带千分位与小数，含正负号与百分号。
_NUM_RE = re.compile(r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?")

#: 建议类句式。命中即归为建议，不强制绑定，但必须能被用户辨识为建议。
_RECOMMEND_HINTS = (
    "建议", "推荐", "可以考虑", "不妨", "更划算", "更稳", "优先选", "思路是",
    "如果预算", "值得", "取舍", "我倾向", "一般做法",
)
#: 推导标记。命中且句中数值均可溯源即视为计算派生。
_DERIVED_HINTS = ("因此", "所以", "计算得", "推导", "合计", "= ", "＝", "换算")

#: 缺据声明句式：陈述「证据里没有某项信息」。
#: 这类句子断言的是信息的**缺失**，本质上无法绑定证据——要求它带引用是自相矛盾的。
#: 实测模型主动声明缺据时被闸门拦掉了，而那正是「无据不答」希望它做的事。
#:
#: 用**正则**而非关键词表：实测两轮各撞一次同类假阳性，只是措辞不同
#: （「未包含」→「不包含」、「本次证据」→「本轮证据」）。枚举词表补不完，
#: 按「否定词 + 提供类动词」的构式匹配才收得住。
_DISCLOSURE_RE = re.compile(
    r"(?:未|不|没(?:有)?|无)\s*(?:包含|涉及|提供|给出|收录|说明|记载|列出)"
    r"|(?:本次|本轮|当前|以上|上述|该批)\s*证据"
    r"|证据(?:中|里)?(?:没有|不含|未)"
    r"|(?:知识库|库)(?:中|里)?(?:没有|查不到|未收录)"
    r"|查不到|无法确认|缺少依据|需(?:另行|进一步)查证"
)

#: 面向用户的操作指引：「请单独提问」「如需…请…」。
#: 这类句子讲的是怎么继续用这个助手，不是在断言游戏事实。
#: 要求**不含数字**——带数字的「如需满抗请堆到 75%」是建议或事实，不是操作指引。
_META_INSTRUCTION_RE = re.compile(
    r"(?:请(?:单独|另行|重新)?(?:提问|询问|告知|补充|指定|说明))"
    r"|(?:如(?:果)?需|若需|需要).{0,16}(?:请|可以)"
    r"|(?:可以再(?:问|提问))|(?:欢迎(?:继续)?提问)"
)

#: 模糊限定语。需求 1.5/15.1 明令禁止用它们把推断包装成模糊表述。
#: 这类词出现在事实句里，即使绑了引用也要拦——它们的作用就是回避确定性。
_HEDGE_WORDS = (
    "一般来说", "通常来说", "通常情况下", "大概是", "大约是", "应该是", "可能是",
    "据我所知", "印象中", "记得是", "差不多是", "多半",
)

#: 工艺台目录 scope 使用“结构化字段正向匹配”，而不是靠扩充同义词黑名单。
#: 下列模式只用于生成可审计的拦截原因；未命中任何模式也会按“目录未覆盖”拒绝。
_BENCH_CATALOG_FORBIDDEN: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("给定装备状态下的可执行性", re.compile(
        r"可执行|执行该工艺|用于当前装备|当前装备.{0,8}(?:使用|制作|应用)"
        r"|(?:能|可以|可).{0,12}(?:使用|制作|应用)"
    )),
    ("装备状态转移或词缀替换/移除", re.compile(
        r"装备状态|状态转移|(?:添加|删除|移除|替换|覆盖|保留).{0,10}(?:词缀|属性|前缀|后缀|crafted[ -]?mod)"
    , re.I)),
    ("制作步骤或操作顺序", re.compile(
        r"制作步骤|操作步骤|操作顺序|制作流程|第一步|第二步|下一步|先.{0,20}再|依次"
    )),
    ("成功率或概率", re.compile(r"成功率|成功机会|成功可能|概率|几率|机率|期望次数|平均次数")),
    ("市场成本或价格", re.compile(r"市场成本|实际成本|市价|市场价|行情|折算价格|交易价格")),
    ("最快、最经济或最优路线", re.compile(
        r"最快|最经济|最省|最便宜|最优|路线|方案比较|成本最低"
    )),
    ("词缀容量、crafted-mod 上限或元词缀交互", re.compile(
        r"词缀容量|crafted[ -]?mod.{0,6}上限|工艺词缀.{0,6}上限|元词缀.{0,8}(?:交互|作用|影响)"
    , re.I)),
)
_BENCH_PURE_BOUNDARY = re.compile(
    r"^(?:该)?目录(?:记录)?(?:不|无法|不能|不足以)证明(?:它)?(?:在)?"
    r"(?:任意|当前|给定|这件|该)?(?:给定)?装备状态下可执行$"
)
_BENCH_EFFECT_CLAIM = re.compile(
    r"^\s*(?:poedb\s*)?(?:页面|目录|页面目录)?\s*(?:列出的?\s*)?"
    r"效果原文\s*(?:是|为|：|:)\s*(.+?)\s*[。！？!?]*$",
    re.I,
)
_BENCH_COST_CLAIM = re.compile(
    r"^\s*(?:poedb\s*)?(?:页面|目录|页面目录)?\s*(?:列出的?\s*)?"
    r"(?:原始费用|页面费用)\s*(?:是|为|：|:)\s*(.+?)\s*[。！？!?]*$",
    re.I,
)
_BENCH_ITEM_CLASSES_CLAIM = re.compile(
    r"^\s*(?:poedb\s*)?(?:页面|目录|页面目录)?\s*(?:列出的?\s*)?"
    r"(?:适用\s*itemclasses|适用类别)\s*(?:是|为|：|:)\s*(.+?)\s*[。！？!?]*$",
    re.I,
)
_BENCH_UNLOCK_CLAIM = re.compile(
    r"^\s*(?:poedb\s*)?(?:页面|目录|页面目录)?\s*(?:列出的?\s*)?"
    r"解锁(?:信息|文本|说明)?\s*(?:是|为|：|:)\s*(.+?)\s*[。！？!?]*$",
    re.I,
)
_BENCH_COST_PAIR = re.compile(
    r"([-+]?\d+(?:\.\d+)?)\s*[xX×]\s*(.+?)(?="
    r"\s*(?:\+|和|与|、|，|,)\s*[-+]?\d+(?:\.\d+)?\s*[xX×]|\s*$)"
)
_BENCH_CATALOG_PRESENCE = re.compile(
    r"^\s*(?:poedb\s*)?(?:页面)?目录(?:中|里)?\s*"
    r"(?:列出|收录|有|存在)了?\s*(?:该选项|这个选项|该目录项)\s*[。！？!?]*$",
    re.I,
)


@dataclass(frozen=True)
class Evidence:
    """进入生成的一条证据。`label` 是注入上下文的编号。"""

    label: str
    chunk_id: str
    entity_id: str | None
    title: str
    url: str
    source: str
    source_priority: int
    text: str
    can_support_facts: bool = True
    #: 证据可支撑的事实范围。`general` 保持既有证据行为；`bench_catalog`
    #: 只能支撑页面目录字段，不能被反向用于证明制作执行语义。
    claim_scope: str = "general"
    #: 受限证据的结构化原字段。范围闸门只按这里的值做正向匹配，不能仅靠关键词。
    claim_data: dict[str, Any] = field(default_factory=dict)

    @property
    def numbers(self) -> set[str]:
        """证据里出现的全部数值，归一后用于回填校验。"""
        return {_norm_num(m.group()) for m in _NUM_RE.finditer(self.text)}


@dataclass
class SentenceVerdict:
    text: str
    kind: SentenceKind
    labels: tuple[str, ...]
    ok: bool
    reason: str
    #: 未通过时代替原句下发的内容
    replacement: str = ""
    #: 未通过时记入缺口台账的缺失信息点
    missing_point: str = ""

    @property
    def output(self) -> str:
        return self.text if self.ok else self.replacement


@dataclass
class BindingResult:
    verdicts: list[SentenceVerdict] = field(default_factory=list)
    #: 被拦截的事实句数，对应指标 leakage_blocked
    blocked: int = 0
    #: 需要写入缺口台账的缺失点
    gaps: list[str] = field(default_factory=list)
    #: 实际被引用到的证据编号，用于底部来源列表（未被引用的不列）
    used_labels: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(v.output for v in self.verdicts)

    @property
    def all_passed(self) -> bool:
        return self.blocked == 0

    def summary(self) -> dict[str, Any]:
        """进轨迹的结构化结果（需求 4.5：拦截原因可追溯）。"""
        return {
            "sentences": len(self.verdicts),
            "blocked": self.blocked,
            "by_kind": {
                k: sum(1 for v in self.verdicts if v.kind == k)
                for k in ("factual", "derived", "recommendation", "narrative")
            },
            "blocked_detail": [
                {"text": v.text[:120], "reason": v.reason}
                for v in self.verdicts if not v.ok
            ],
            "used_labels": self.used_labels,
        }


# ── 证据编号 ───────────────────────────────────────────────

def number_evidence(citations: Iterable[dict[str, Any]]) -> list[Evidence]:
    """给证据分配 `E1..En`。同一 chunk 只编一次，避免重复占号。"""
    out: list[Evidence] = []
    seen: set[str] = set()
    for c in citations:
        cid = str(c.get("chunk_id") or "")
        if cid and cid in seen:
            continue
        seen.add(cid)
        priority = int(c.get("source_priority") or 99)
        out.append(
            Evidence(
                label=f"E{len(out) + 1}",
                chunk_id=cid,
                entity_id=c.get("entity_id"),
                title=str(c.get("title") or ""),
                url=str(c.get("url") or ""),
                source=str(c.get("source") or ""),
                source_priority=priority,
                text=str(c.get("text") or ""),
                can_support_facts=bool(c.get("can_support_facts", True)),
                claim_scope=str(c.get("claim_scope") or "general"),
                claim_data=(
                    dict(c["claim_data"])
                    if isinstance(c.get("claim_data"), dict)
                    else {}
                ),
            )
        )
    return out


def render_evidence_block(evidence: list[Evidence]) -> str:
    """渲染进提示词的证据块。**不含 URL**——引用格式由代码在出口装配，
    给了 URL 模型就会自己编引用。"""
    parts = []
    for e in evidence:
        tags: list[str] = []
        if not e.can_support_facts:
            tags.append("社区来源，不可支撑事实断言")
        if e.claim_scope == "bench_catalog":
            tags.append("仅可支撑目录存在、效果原文、原始费用、ItemClasses 与解锁信息")
        tag = f"（{'；'.join(tags)}）" if tags else ""
        parts.append(f"[{e.label}] 来源：{e.source} → {e.title}{tag}\n{e.text}")
    return "\n\n".join(parts)


# ── 校验 ───────────────────────────────────────────────────

def verify(
    answer: str,
    evidence: list[Evidence],
    *,
    factual_terms: Iterable[str] = (),
    domain_terms: Iterable[str] = (),
) -> BindingResult:
    """逐句校验并拦截。返回的 `text` 是可安全下发的内容。

    `factual_terms` 来自 config.yaml 的领域词表；`domain_terms` 来自库里的
    实体名与底材类别（`retrieval/vocab.py` 生成）。两者都用于事实句判定——
    命中即要求绑定引用。
    """
    by_label = {e.label.upper(): e for e in evidence}
    terms = tuple(t for t in factual_terms if t)
    domain = tuple(t for t in domain_terms if t)
    result = BindingResult()
    used: list[str] = []

    for raw in split_sentences(answer):
        if not raw.strip():
            result.verdicts.append(
                SentenceVerdict(raw, "narrative", (), True, "空白")
            )
            continue
        verdict = _check_sentence(raw, by_label, terms, domain)
        result.verdicts.append(verdict)
        if verdict.ok:
            for lb in verdict.labels:
                if lb not in used:
                    used.append(lb)
        else:
            result.blocked += 1
            if verdict.missing_point:
                result.gaps.append(verdict.missing_point)

    # 第二遍：派生句必须有「已通过校验的事实句」作为推导输入。
    # 否则模型只要在编造的数值前加个「因此」就能绕过整道闸门。
    has_grounded_input = any(
        v.ok and v.kind == "factual" and v.labels for v in result.verdicts
    )
    if not has_grounded_input:
        for i, v in enumerate(result.verdicts):
            if v.kind != "derived" or not v.ok or "未溯源数值" not in v.reason:
                continue
            blocked = _block(
                v.text, v.kind, v.labels,
                "计算派生句的数值无法溯源，且整个回答中没有可作为推导输入的有据事实句",
                f"推导链缺少可溯源的输入：{_excerpt(_BINDING_RE.sub('', v.text))}",
            )
            result.verdicts[i] = blocked
            result.blocked += 1
            result.gaps.append(blocked.missing_point)

    result.used_labels = [
        lb for v in result.verdicts if v.ok for lb in v.labels
    ]
    # 去重保序
    seen_lb: set[str] = set()
    result.used_labels = [
        lb for lb in result.used_labels
        if not (lb in seen_lb or seen_lb.add(lb))  # type: ignore[func-returns-value]
    ]
    return result


class StreamingGate:
    """增量版出口闸门：喂 token 分片，产出**已校验通过**的整句。

    为什么流式不能逐字下发：校验单位是**句子**——要判断一句是不是事实断言、
    绑定的编号是否有效、句中数值能否溯源，都得等这句说完。逐字推送等于把
    未校验内容先给了用户，之后再撤回；而撤回是做不到的，用户已经读到了。

    所以流式的粒度必然是句。前端可以对已通过的句子做打字机效果，
    观感接近逐字流，但**不会显示任何未校验内容**。

    与批量 `verify()` 必须行为一致：这里复用同一套分句与校验函数，
    不另写一份规则。两份规则迟早分叉，那时流式与非流式的结果就不一样了。
    """

    def __init__(
        self,
        evidence: list[Evidence],
        *,
        factual_terms: Iterable[str] = (),
        domain_terms: Iterable[str] = (),
    ) -> None:
        self._by_label = {e.label.upper(): e for e in evidence}
        self._terms = tuple(t for t in factual_terms if t)
        self._domain = tuple(t for t in domain_terms if t)
        self._buffer = ""
        self.verdicts: list[SentenceVerdict] = []
        self.blocked = 0
        self.gaps: list[str] = []

    # ── 证据 ──

    def add_evidence(self, evidence: list[Evidence]) -> None:
        """补进新检索到的证据。

        编号是**追加式**的（`_merge_evidence` 不重编已有 chunk），所以这里只需
        把新编号补进映射。若重编号，模型先前标的 `[[E1]]` 就会指向别的东西，
        已下发的句子也无法追回。
        """
        for e in evidence:
            self._by_label.setdefault(e.label.upper(), e)

    # ── 消费 ──

    def feed(self, delta: str) -> list[SentenceVerdict]:
        """喂一个分片，返回本次新完成校验的句子（可能为空）。"""
        if not delta:
            return []
        self._buffer += delta
        return self._drain(final=False)

    def flush(self) -> list[SentenceVerdict]:
        """收尾：把缓冲区剩余内容当作最后一句处理（可能没有终止标点）。"""
        return self._drain(final=True)

    # ── 内部 ──

    def _drain(self, *, final: bool) -> list[SentenceVerdict]:
        out: list[SentenceVerdict] = []
        while True:
            piece, rest = self._take_sentence(final=final)
            if piece is None:
                break
            self._buffer = rest
            verdict = _check_sentence(piece, self._by_label, self._terms, self._domain)
            self.verdicts.append(verdict)
            if not verdict.ok:
                self.blocked += 1
                if verdict.missing_point:
                    self.gaps.append(verdict.missing_point)
            out.append(verdict)
        return out

    def _take_sentence(self, *, final: bool) -> tuple[str | None, str]:
        """从缓冲区切出一个完整句子。切不出来返回 (None, 原缓冲)。"""
        buf = self._buffer
        if not buf:
            return None, buf

        # 绑定标记未闭合时**绝不切**：`[[E1;E2]]` 只写到 `[[E1;` 时，
        # 那个分号会被当成句末标点，切出来的半个标记会让有据句被判成未绑定。
        if _has_open_marker(buf):
            return (buf, "") if final else (None, buf)

        parts = split_sentences(buf)
        if not parts:
            return None, buf
        if final:
            # 收尾时全部交出。多于一句就先给第一句，其余留待下一轮循环。
            return parts[0], "".join(parts[1:])
        if len(parts) == 1 and not _ends_sentence(buf):
            # 只有一段且没说完，继续等
            return None, buf
        if len(parts) == 1:
            return parts[0], ""
        return parts[0], "".join(parts[1:])

    # ── 结果 ──

    def result(self) -> BindingResult:
        """汇总成与批量校验同构的结果，供轨迹落库与引用装配复用。"""
        res = BindingResult(verdicts=list(self.verdicts), blocked=self.blocked,
                            gaps=list(self.gaps))
        seen: set[str] = set()
        used: list[str] = []
        for v in res.verdicts:
            if not v.ok:
                continue
            for lb in v.labels:
                if lb not in seen:
                    seen.add(lb)
                    used.append(lb)
        res.used_labels = used
        return res


def _has_open_marker(text: str) -> bool:
    """缓冲区末尾是否有未闭合的 `[[`。"""
    open_at = text.rfind("[[")
    if open_at < 0:
        return False
    return text.find("]]", open_at) < 0


_SENT_END_CHARS = "。！？!?；;\n"


def _ends_sentence(text: str) -> bool:
    return bool(text) and text.rstrip()[-1:] in _SENT_END_CHARS or text.endswith("\n")


def split_sentences(text: str) -> list[str]:
    """分句，保留原有标点与空白，拼回去应与原文等价（除拦截替换）。

    保序保空白是有意的：出口要按句缓冲下发，切一次再拼一次不能改动内容，
    否则用户看到的文本与校验过的文本就不是同一份了。

    绑定标记内部不切（实测踩到）：`[[E1;E2]]` 里的 `;` 会被当成句末标点，
    切开后标记失效，一条有据的句子被判成未绑定引用——这是错误拒答。
    """
    if not text:
        return []
    protected = [(m.start(), m.end()) for m in _BINDING_RE.finditer(text)]

    def inside_marker(idx: int) -> bool:
        return any(lo < idx < hi for lo, hi in protected)

    out: list[str] = []
    pos = 0
    for m in _SENT_SPLIT_RE.finditer(text):
        end = m.end()
        if inside_marker(m.start()) or inside_marker(end):
            continue
        piece = text[pos:end]
        if piece:
            out.append(piece)
        pos = end
    if pos < len(text):
        out.append(text[pos:])
    return out


def _claim_value_norm(value: Any) -> str:
    return re.sub(r"[\s。！？!?，,、；;：:]", "", str(value or "")).lower()


def _parse_bench_cost_claim(value: str) -> list[tuple[str, str]] | None:
    """完整消费费用字段；任何未被费用对或允许分隔符覆盖的文本都拒绝。"""
    matches = list(_BENCH_COST_PAIR.finditer(value))
    if not matches:
        return None
    out: list[tuple[str, str]] = []
    cursor = 0
    for index, match in enumerate(matches):
        gap = value[cursor:match.start()]
        if index == 0:
            if gap.strip():
                return None
        elif not re.fullmatch(r"\s*(?:\+|和|与|、|，|,)\s*", gap):
            return None
        out.append((_norm_num(match.group(1)), _claim_value_norm(match.group(2))))
        cursor = match.end()
    if value[cursor:].strip():
        return None
    return out


def _bench_catalog_scope_error(
    sentence: str,
    evidence: list[Evidence],
    kind: SentenceKind,
) -> str:
    """正向核对目录字段；任何未精确匹配的语义都 fail closed。"""
    normalized = sentence.strip()
    compact = _claim_value_norm(normalized)
    claim_data = [e.claim_data for e in evidence if e.claim_data]

    # Catalog v1 不包含“采用某选项”的建议依据；建议也不能借目录 Citation 解锁。
    if kind == "recommendation":
        return "制作建议或选项采用结论"

    if _BENCH_PURE_BOUNDARY.fullmatch(compact):
        return ""

    effect_match = _BENCH_EFFECT_CLAIM.fullmatch(normalized)
    if effect_match:
        claimed = _claim_value_norm(effect_match.group(1))
        effects = {
            _claim_value_norm(data.get("effect_text"))
            for data in claim_data
            if data.get("effect_text")
        }
        if claimed and claimed in effects:
            return ""
        return "与 Citation 不一致的效果原文"

    cost_match = _BENCH_COST_CLAIM.fullmatch(normalized)
    if cost_match:
        claimed_costs = _parse_bench_cost_claim(cost_match.group(1))
        expected_cost_lists = [
            [
                (_norm_num(str(cost.get("amount"))), _claim_value_norm(cost.get("currency")))
                for cost in (data.get("raw_costs") or [])
                if isinstance(cost, dict) and cost.get("amount") is not None
                and cost.get("currency")
            ]
            for data in claim_data
        ]
        if claimed_costs and any(
            claimed_costs == expected for expected in expected_cost_lists if expected
        ):
            return ""
        return "与 Citation 不一致的原始费用"

    item_classes_match = _BENCH_ITEM_CLASSES_CLAIM.fullmatch(normalized)
    if item_classes_match:
        claimed_classes = {
            _claim_value_norm(value)
            for value in re.split(
                r"(?:和|与|、|，|,|\+)", item_classes_match.group(1)
            )
            if _claim_value_norm(value)
        }
        expected_class_sets = [
            {
                _claim_value_norm(item_class)
                for item_class in (data.get("item_classes") or [])
                if item_class
            }
            for data in claim_data
        ]
        if claimed_classes and any(
            claimed_classes <= expected for expected in expected_class_sets if expected
        ):
            return ""
        return "与 Citation 不一致的 ItemClasses"

    unlock_match = _BENCH_UNLOCK_CLAIM.fullmatch(normalized)
    if unlock_match:
        claimed_unlock = _claim_value_norm(unlock_match.group(1))
        unlock_values = {
            _claim_value_norm(value)
            for data in claim_data
            for value in (data.get("unlock_text"), data.get("unlock_hint"))
            if value
        }
        if claimed_unlock and claimed_unlock in unlock_values:
            return ""
        return "与 Citation 不一致的解锁信息"

    if _BENCH_CATALOG_PRESENCE.fullmatch(normalized):
        return ""

    for claim_name, pattern in _BENCH_CATALOG_FORBIDDEN:
        if pattern.search(normalized):
            return claim_name
    return "目录未明确覆盖的事实语义"


def _check_sentence(
    sentence: str,
    by_label: dict[str, Evidence],
    terms: tuple[str, ...],
    domain: tuple[str, ...] = (),
) -> SentenceVerdict:
    labels = extract_labels(sentence)
    stripped = _BINDING_RE.sub("", sentence)
    kind = classify(stripped, labels, terms, domain)

    if kind == "narrative" and not labels:
        return SentenceVerdict(sentence, kind, labels, True, "无需绑定")
    if kind == "recommendation" and not labels:
        # 纯建议不强制绑定；一旦主动绑定证据，仍须检查该证据的能力范围。
        return SentenceVerdict(sentence, kind, labels, True, "无需绑定")

    hedge = next((w for w in _HEDGE_WORDS if w in stripped), "")
    if hedge:
        return _block(
            sentence, kind, labels,
            f"含模糊限定语「{hedge}」。需求 1.5/15.1 禁止用它把推断包装成模糊表述",
            f"该点被模糊表述掩盖，需要确切依据：{_excerpt(stripped)}",
        )

    if not labels:
        return _block(
            sentence, kind, labels,
            "事实句未绑定引用",
            f"缺少可引用依据：{_excerpt(stripped)}",
        )

    unknown = [lb for lb in labels if lb not in by_label]
    if unknown:
        return _block(
            sentence, kind, labels,
            f"引用编号无效：{', '.join(unknown)}（可用 {', '.join(by_label) or '无'}）",
            f"引用编号无效，依据无法核实：{_excerpt(stripped)}",
        )

    bound = [by_label[lb] for lb in labels]
    restricted = [e for e in bound if e.claim_scope == "bench_catalog"]
    if restricted:
        scope_error = _bench_catalog_scope_error(stripped, restricted, kind)
        if scope_error:
            return _block(
                sentence, kind, labels,
                f"工艺台目录证据仅支撑页面目录字段，不能支撑{scope_error}",
                f"缺少可支撑{scope_error}的非目录证据：{_excerpt(stripped)}",
            )

    weak = [e for e in bound if not e.can_support_facts]
    if weak and len(weak) == len(bound):
        return _block(
            sentence, kind, labels,
            f"绑定的来源属社区级，不可支撑事实断言"
            f"（{weak[0].source}，P{weak[0].source_priority}）",
            f"仅有社区来源，未经权威来源证实：{_excerpt(stripped)}",
        )

    if kind in ("recommendation", "narrative"):
        return SentenceVerdict(sentence, kind, labels, True, "已通过证据范围校验")

    # 数值回填校验：句中数值必须能在绑定证据里找到。
    # 编造的具体形态就是数字，这一条抓得最准。
    missing = unsupported_numbers(stripped, bound)

    if kind == "derived":
        # 计算派生（需求 15.3、10）：结论数值本来就不在证据里——那是算出来的，
        # 而输入往往在**上一句**。所以不能要求同句内有可溯源数值。
        #
        # 但「因此」二字不能成为绕过闸门的后门：`verify` 的第二遍会检查整个回答里
        # 是否存在已通过校验的事实句作为推导输入，没有就把派生句一并拦掉。
        #
        # 待办（任务 3.5）：`calc/` 就位后派生数值应由计算器产出并回填校验，
        # 届时这里改为强校验，不再依赖「有事实句垫底」这种间接约束。
        return SentenceVerdict(
            sentence, kind, labels, True,
            "派生" + (f"，未溯源数值 {', '.join(missing)}" if missing else ""),
            missing_point="",
        )

    if missing:
        return _block(
            sentence, kind, labels,
            f"数值 {', '.join(missing)} 未出现在绑定证据 {', '.join(labels)} 中",
            f"数值 {', '.join(missing)} 无法溯源：{_excerpt(stripped)}",
        )

    return SentenceVerdict(sentence, kind, labels, True, "通过")


def _block(
    sentence: str, kind: SentenceKind, labels: tuple[str, ...],
    reason: str, missing_point: str,
) -> SentenceVerdict:
    return SentenceVerdict(
        text=sentence,
        kind=kind,
        labels=labels,
        ok=False,
        reason=reason,
        replacement=f"〔此处原有一条无据断言，已被拦截：{reason}〕",
        missing_point=missing_point,
    )


def extract_labels(sentence: str) -> tuple[str, ...]:
    out: list[str] = []
    for m in _BINDING_RE.finditer(sentence):
        for num in _LABEL_RE.finditer(m.group(1)):
            lb = f"E{num.group(1)}"
            if lb not in out:
                out.append(lb)
    return tuple(out)


def classify(
    sentence: str,
    labels: tuple[str, ...],
    terms: tuple[str, ...],
    domain_terms: tuple[str, ...] = (),
) -> SentenceKind:
    """规则优先的事实句判定。**默认按事实句处理。**

    顺序有讲究：建议先判，因为「建议把抗性堆到 75%」既含建议词也含数值与领域词，
    应归建议（附假设即可），不该被当成事实断言强制绑定。

    默认值是 `factual` 而不是 `narrative`——这是实测改过来的。早先判不出类型就
    当过渡句放行，结果「猎首还会让你免疫冰冻」这种既无数值也无领域词的编造机制句
    直接漏出。那正是本闸门最该拦的东西。所以过渡句改成**白名单**：只有匹配纯元话语
    句式的才放行，其余一律按事实句要求绑定引用。

    代价是像「另外还有一点。」这类无害的连接句也会被要求绑定。在「漏出无据断言」与
    「多拦一句过渡语」之间，本项目明确选后者（输出错误内容的代价高于输出查不到）。
    """
    s = sentence.strip()
    if not s:
        return "narrative"
    # Markdown 排版优先判：它比领域词命中更具体。
    # 标题里出现「猎首」不代表这句在断言什么，只是个小节标签。
    if is_structural(s):
        return "narrative"
    has_hedge = any(w in s for w in _HEDGE_WORDS)
    # 缺据声明：断言的是信息缺失，无法也不该绑定证据。
    # 但必须同时不含模糊限定语——否则「证据里没写，一般来说是 100」会整句蒙过去。
    if _DISCLOSURE_RE.search(s) and not has_hedge:
        return "narrative"
    # 面向用户的操作指引。要求不含数字：带数字的是建议或事实，不是指引。
    if _META_INSTRUCTION_RE.search(s) and not has_hedge and not _NUM_RE.search(s):
        return "narrative"
    if any(h in s for h in _RECOMMEND_HINTS):
        return "recommendation"
    if any(h in s for h in _DERIVED_HINTS) and _NUM_RE.search(s):
        return "derived"
    if labels:
        # 模型自己标了引用，说明它认为这是事实句 —— 按事实句校验
        return "factual"
    if any(t in s for t in terms) or any(t in s for t in domain_terms):
        return "factual"
    if _NUM_RE.search(s):
        return "factual"
    if _NARRATIVE_RE.match(s):
        return "narrative"
    return "factual"


#: 有实义内容：至少含一个中日韩字符、拉丁字母或数字。
#: 只剩标点的碎片（分句时被切散的孤立 `）`）不承载断言。
_HAS_CONTENT_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ffA-Za-z0-9]")


def is_structural(sentence: str) -> bool:
    """是否为 Markdown 排版元素（标题 / 加粗标签 / 分隔线 / 空列表符号）
    或无实义内容的碎片。

    带数字的标题一律返回 False，交回事实句校验——数值是编造的主要载体。
    """
    s = sentence.strip()
    if not s:
        return True
    if not _HAS_CONTENT_RE.search(s):
        # 纯标点碎片。实测括号内含句号时会被切出一个孤立的「）」，
        # 按事实句处理就会在答案里插一条莫名的拦截说明。
        return True
    if _MD_RULE_RE.match(s) or _MD_BULLET_ONLY_RE.match(s):
        return True
    for pattern in (_MD_HEADING_RE, _MD_LABEL_RE):
        m = pattern.match(s)
        if m and not _NUM_RE.search(m.group(1)):
            return True
    return False


def unsupported_numbers(sentence: str, bound: list[Evidence]) -> list[str]:
    """找出句中无法在绑定证据里溯源的数值。

    证据里的区间 `(40-55)` 覆盖句中的具体值 `47`：模型从区间里取一个具体数
    是合理表述，不该判为编造。所以除了逐字匹配，还要做区间包含判断。
    """
    pool: set[str] = set()
    ranges: list[tuple[float, float]] = []
    for e in bound:
        pool |= e.numbers
        ranges.extend(_ranges_in(e.text))

    missing: list[str] = []
    for m in _NUM_RE.finditer(sentence):
        raw = m.group()
        norm = _norm_num(raw)
        if norm in pool:
            continue
        val = _to_float(raw)
        if val is not None and any(lo <= val <= hi for lo, hi in ranges):
            continue
        if norm not in missing:
            missing.append(raw)
    return missing


_RANGE_IN_TEXT_RE = re.compile(
    r"\(\s*([-+]?\d+(?:\.\d+)?)\s*[-—–~]\s*([-+]?\d+(?:\.\d+)?)\s*\)"
)


def _ranges_in(text: str) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for m in _RANGE_IN_TEXT_RE.finditer(text):
        a, b = _to_float(m.group(1)), _to_float(m.group(2))
        if a is not None and b is not None:
            out.append((min(a, b), max(a, b)))
    return out


def _norm_num(raw: str) -> str:
    """归一数值文本，使 `1,000`、`1000`、`1000.0` 视为同一个数。"""
    val = _to_float(raw)
    if val is None:
        return raw
    return str(int(val)) if float(val).is_integer() else str(val)


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", "").lstrip("+"))
    except (TypeError, ValueError):
        return None


def _excerpt(sentence: str, limit: int = 60) -> str:
    s = sentence.strip()
    return s if len(s) <= limit else s[:limit] + "…"
