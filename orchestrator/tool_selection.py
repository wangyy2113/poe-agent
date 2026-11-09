"""Expose focused tool schemas without turning a keyword hint into a lock."""
from __future__ import annotations

from typing import Any
import re
from copy import deepcopy

from agents.knowledge import ALLOWED_TOOLS
from orchestrator.answer import SCHEMA as ANSWER_SCHEMA
from tools.registry import REGISTRY, ToolResult

SELECT_TOOL = "select_tools"
SEARCH_TOOLS = ("kb_search", "entity_lookup")


def schemas(names: list[str], *, precision: bool = False, initial: bool = False) -> list[dict[str, Any]]:
    catalogue = list(dict.fromkeys([*SEARCH_TOOLS,
        *(name for name in ALLOWED_TOOLS if name.endswith("_calc")), *ALLOWED_TOOLS]))
    result = deepcopy(REGISTRY.schemas(list(dict.fromkeys(names))))
    for schema in result:
        function = schema["function"]
        if function["name"].endswith("_calc"):
            function["parameters"]["properties"].setdefault("unsupported_conditions", {
                "type": "array", "items": {"type": "string"},
                "description": "仅列用户明确给出、会影响所问目标但工具无法表达的条件。未给出的可选参数、无关分支不是unsupported，不要填入。非空时报告限制，不执行简化计算。",
            })
    if precision:
        return result
    result.append(deepcopy(ANSWER_SCHEMA))
    result.append({
        "type": "function", "function": {
            "name": SELECT_TOOL,
            "description": (
                "先理解原问题，再选择工具。purpose应明确用户要查询/计算的对象、动作、条件和全部子问题，不能仅复述关键词。"
                "选择本轮必须执行的工具；选中的工具必须先调用核对，才能完成事实回答。"
                "可补选工具回答复合问题、比较或修正初始路由。"
                "检索未命中时可先用kb_search改写关键词，已知实体名用entity_lookup核实。"
                "选择工具不会执行计算或提供事实证据。\n工具目录：\n"
                + ("\n".join(f"{spec.name}: {re.split(r'[。\n]', spec.description)[0][:100]}"
                             for name in catalogue if (spec := REGISTRY.spec(name))) if initial else
                   "按名称补选；完整参数会在下一轮提供。")
            ),
            "parameters": {
                "type": "object", "properties": {
                    "purpose": {"type": "string", "description": "按完整语义说明目标、条件及各子问题；保留否定和上下文修改。"},
                    "names": {"type": "array", "items": {"type": "string", "enum": ALLOWED_TOOLS},
                              "minItems": 1, "maxItems": 4},
                }, "required": ["purpose", "names"], "additionalProperties": False,
            },
        },
    })
    return result


def select(arguments: Any) -> ToolResult:
    names = arguments.get("names") if isinstance(arguments, dict) else None
    if (not isinstance(names, list) or not 1 <= len(names) <= 4
            or any(not isinstance(n, str) or n not in ALLOWED_TOOLS for n in names)):
        return ToolResult(ok=False, error="请选择1至4个目录内的工具名称")
    return ToolResult(ok=True, data={"enabled": list(dict.fromkeys(names))})
