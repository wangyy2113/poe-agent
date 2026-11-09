"""Compose grounded answers from cited statements and immutable source blocks."""
from __future__ import annotations

from typing import Any
import re
from copy import deepcopy

from orchestrator.binding import BindingResult, Evidence, SentenceVerdict, split_sentences, verify
from orchestrator.excerpts import source_quotes

FINISH_TOOL = "finish_answer"
SCHEMA = {
    "type": "function", "function": {
        "name": FINISH_TOOL,
        "description": (
            "完成本轮回答，覆盖原问题各个目标。"
            "原文摘录证据只能用quote选择直接解答目标的字段或计算结果，不能用text改写。"
            "不输出无关候选及其淘汰原因。只有未限定为原文摘录的证据才可用text和citations解释。"
            "澄清用ask，只填需要用户补充/确认的信息名称，不写游戏结论。缺口用text。不能把没查到改成不存在。"
            "整题缺乏资料时直接status=gap、blocks=[]，系统会给出缺据说明，无需编写解释或附无关资料。"
            "工具结果未覆盖的条件须明确说明，不能以简化结果冒充完整答案。"
        ),
        "parameters": {
            "type": "object", "properties": {
                "status": {"type": "string", "enum": ["answer", "clarification", "gap"]},
                "blocks": {"type": "array", "minItems": 0, "maxItems": 24,
                           "items": {"type": "object", "properties": {
                               "ask": {"type": "string", "maxLength": 120, "description": "仅填待确认的字段名，例如当前赛季、职业与升华、实际最大火抗；不填数值、建议取值、算例或机制解释。仅用于clarification。"},
                               "report": {"type": "string", "description": "可信工具报告ID（仅工具明确返回的report），与quote、text互斥"},
                               "quote": {"type": "string", "description": "完整原文段落ID，如Q1.2；与text二选一"},
                               "text": {"type": "string", "description": "缺据说明或有非摘录证据支持的解释；摘录事实使用quote，不手写引用标记"},
                               "citations": {"type": "array", "items": {"type": "string"},
                                             "description": "text依据的证据编号，如[E1,E2]；仅标题或澄清可为空"},
                           }, "additionalProperties": False}},
            }, "required": ["status", "blocks"], "additionalProperties": False,
        },
    },
}


def schema(evidence: list[Evidence]) -> dict:
    """Only offer answer operations that the current evidence can support."""
    result = deepcopy(SCHEMA)
    properties = result["function"]["parameters"]["properties"]["blocks"]["items"]["properties"]
    quotes = source_quotes(evidence)
    if quotes:
        properties["quote"]["enum"] = list(quotes)
    else:
        properties.pop("quote")
    if not any(e.can_support_facts and e.claim_scope != "excerpt" for e in evidence):
        # Excerpts cannot license paraphrases. Do not offer a text operation
        # that can only fail the binding gate and consume another model round.
        properties.pop("text")
        properties.pop("citations")
    else:
        properties["citations"]["items"]["enum"] = [e.label for e in evidence if e.can_support_facts]
    return result


def compose(arguments: Any, evidence: list[Evidence], *, reports: dict[str, str] | None = None) -> tuple[str, BindingResult]:
    if not isinstance(arguments, dict) or arguments.get("status") not in {"answer", "clarification", "gap"}:
        raise ValueError("请提供answer、clarification或gap状态")
    blocks = arguments.get("blocks")
    if arguments["status"] == "gap" and blocks == []:
        text = "本轮未找到足以回答该问题的可引用依据，暂时无法给出结论。可补充规范名称、原文或具体条件后继续核对。"
        return text, BindingResult(verdicts=[SentenceVerdict(text, "narrative", (), True, "缺据说明")])
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 24:
        raise ValueError("回答必须包含1至24个段落")
    quotes, labels = source_quotes(evidence), {e.label for e in evidence}
    rendered, binding = [], BindingResult()
    last_subject = None
    has_report = False
    for block in blocks:
        if not isinstance(block, dict) or sum(bool(block.get(k)) for k in ("quote", "text", "report", "ask")) != 1:
            raise ValueError("每段只能提供quote、text、report或ask之一")
        if block.get("ask"):
            if arguments["status"] != "clarification" or not isinstance(block["ask"], str):
                raise ValueError("ask只能用于clarification，内容须为待补充或确认的信息")
            if len(block["ask"]) > 120 or re.search(r"[0-9%％]", block["ask"]):
                raise ValueError("ask只填写简短的待确认字段名，不填数值或算例；数值与机制说明必须使用有据的quote/text")
            text = "请补充或确认：" + block["ask"].strip().rstrip("。？?") + "？"
            checked = BindingResult(verdicts=[SentenceVerdict(text, "narrative", (), True, "澄清问题")])
        elif block.get("report"):
            text = (reports or {}).get(block["report"])
            if not text:
                raise ValueError("报告不存在，请先调用对应工具")
            checked = BindingResult(verdicts=[SentenceVerdict(text, "narrative", (), True, "可信工具报告")])
            has_report = True
        elif block.get("quote"):
            quote = quotes.get(block["quote"])
            if quote is None:
                raise ValueError("选择的原文段落不存在，请使用证据中的Q编号")
            text = f"{quote.text}[[{quote.evidence.label}]]"
            subject = (quote.evidence.label, quote.heading)
            if last_subject != subject:
                title = quote.evidence.title
                if quote.heading != title:
                    title += f" · {quote.heading}"
                text = f"### {title}\n\n" + text
                last_subject = subject
            # The full immutable source passage was selected by ID. Re-splitting
            # punctuation would orphan citations inside tables/English quotes.
            checked = BindingResult(
                verdicts=[SentenceVerdict(text, "factual", (quote.evidence.label,), True, "完整原文段落")],
                used_labels=[quote.evidence.label],
            )
        else:
            text, refs = block["text"], block.get("citations", [])
            if not isinstance(text, str) or not isinstance(refs, list) or any(ref not in labels for ref in refs):
                raise ValueError("text必须是文本，citations只能使用本轮已提供的E编号")
            if "[[" in text:
                raise ValueError("请将引用填入citations，将原文选择填入quote，不要在text中手写标记")
            marker = "".join(f"[[{ref}]]" for ref in dict.fromkeys(refs))
            text = "".join(piece.rstrip() + marker + "\n" for piece in split_sentences(text) if piece.strip())
            checked = verify(text, evidence)
        if rendered:
            binding.verdicts.append(SentenceVerdict("\n\n", "narrative", (), True, "段落间隔"))
        rendered.append(text)
        binding.verdicts.extend(checked.verdicts)
        binding.blocked += checked.blocked
        binding.gaps.extend(checked.gaps)
        binding.used_labels = list(dict.fromkeys([*binding.used_labels, *checked.used_labels]))
    if arguments["status"] == "answer" and not binding.used_labels and not binding.blocked and not has_report:
        raise ValueError("事实回答需要可引用依据；未找到依据请选择gap，缺输入请选择clarification")
    return "\n\n".join(rendered).strip(), binding
