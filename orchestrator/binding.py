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
#: 实测模型主动声明「本次证据中未包含掉落来源与市场价格」时被闸门拦掉了，
#: 而那正是「无据不答」希望模型做的事，拦它等于惩罚正确行为。
_DISCLOSURE_HINTS = (
    "未包含", "未涉及", "没有涉及", "未提供", "未给出", "查不到", "无法确认",
    "需另行查证", "需进一步查证", "证据中没有", "知识库中没有", "缺少依据",
    "本次证据", "以上证据",
)

#: 模糊限定语。需求 1.5/15.1 明令禁止用它们把推断包装成模糊表述。
#: 这类词出现在事实句里，即使绑了引用也要拦——它们的作用就是回避确定性。
_HEDGE_WORDS = (
    "一般来说", "通常来说", "通常情况下", "大概是", "大约是", "应该是", "可能是",
    "据我所知", "印象中", "记得是", "差不多是", "多半",
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
            )
        )
    return out


def render_evidence_block(evidence: list[Evidence]) -> str:
    """渲染进提示词的证据块。**不含 URL**——引用格式由代码在出口装配，
    给了 URL 模型就会自己编引用。"""
    parts = []
    for e in evidence:
        tag = "" if e.can_support_facts else "（社区来源，不可支撑事实断言）"
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


def _check_sentence(
    sentence: str,
    by_label: dict[str, Evidence],
    terms: tuple[str, ...],
    domain: tuple[str, ...] = (),
) -> SentenceVerdict:
    labels = extract_labels(sentence)
    stripped = _BINDING_RE.sub("", sentence)
    kind = classify(stripped, labels, terms, domain)

    if kind in ("narrative", "recommendation"):
        # 建议类不强制绑定（需求 15.4），但其中的事实前提若带了标注仍会被校验
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
    weak = [e for e in bound if not e.can_support_facts]
    if weak and len(weak) == len(bound):
        return _block(
            sentence, kind, labels,
            f"绑定的来源属社区级，不可支撑事实断言"
            f"（{weak[0].source}，P{weak[0].source_priority}）",
            f"仅有社区来源，未经权威来源证实：{_excerpt(stripped)}",
        )

    # 数值回填校验：句中数值必须能在绑定证据里找到。
    # 编造的具体形态就是数字，这一条抓得最准。
    missing = unsupported_numbers(stripped, bound)
    numbers_total = len(_NUM_RE.findall(stripped))

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
    # 缺据声明：断言的是信息缺失，无法也不该绑定证据。
    # 但必须同时不含模糊限定语——否则「证据里没写，一般来说是 100」会整句蒙过去。
    if any(h in s for h in _DISCLOSURE_HINTS) and not any(w in s for w in _HEDGE_WORDS):
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
