"""批量与流式两条路径的行为一致性（2026-08-18）。

## 来历

`run_turn` 与 `stream_turn` 曾各写一遍同一条流水线，各约 300 行。代价实测过两次：

  1. 表格继承那次改动让「流式与批量判定一致」从 32/32 掉到 18/32——两处分句逻辑分叉。
  2. **结构性漏改**：「偏好工具零证据兜底」只写在批量路径里。流式下同样的问题会带着
     零条证据进闸门、每个事实句被拦，而批量那段注释自己写着这种结果
     「比一句查不到更糟，而且掩盖了本轮其实无据这个事实」。

`tests/unit/test_streaming_gate.py` 钉的是**闸门**的一致性（分句与判定），钉不住
**编排**的一致性——上面第 2 条就是从它的盲区里漏过去的。所以这里按编排层来钉。

## 钉什么

重构后两条路径共用 `_prepare_turn`（准备阶段）、`_handle_tool_call`（单次工具调用）、
`_terminal_gap`（四种确定性拒答）。它们**只允许在「怎么把文本交给用户」上不同**。
下面每个用例都同时跑两条路径并比对结论，而不是只测其中一条。
"""

from __future__ import annotations

import json
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.llm import LLMResponse, StreamEvent  # noqa: E402
from orchestrator import loop  # noqa: E402
from tools.registry import ToolResult  # noqa: E402


class _FakeConfig:
    _VALUES = {
        "llm.max_tokens_per_turn": 1000,
        "llm.max_tool_calls": 2,
        "retrieval.factual_terms": [],
    }

    def get(self, key, default=None):
        return self._VALUES.get(key, default)


class _ChatClient:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = iter(responses)

    def chat(self, _messages, **_kwargs):
        return next(self._responses)


class _StreamClient:
    def __init__(self, rounds: list[list[StreamEvent]]) -> None:
        self._rounds = iter(rounds)

    def stream(self, _messages, **_kwargs):
        yield from next(self._rounds)


class _NeverCalledClient:
    """两条路径都不该调模型时用它——被调到就直接失败，比断言更早暴露问题。"""

    def chat(self, *_a, **_k):  # noqa: ANN002
        raise AssertionError("本轮不应该调用模型")

    def stream(self, *_a, **_k):  # noqa: ANN002
        raise AssertionError("本轮不应该调用模型")
        yield  # pragma: no cover  让它成为生成器


def _answer_of(events: list[dict]) -> str:
    """把流式事件拼回文本。`gap` 与 `sentence` 都算正文，`blocked` 取替换文本。"""
    parts = []
    for ev in events:
        if ev.get("type") in {"gap", "sentence"}:
            parts.append(ev["text"])
        elif ev.get("type") == "blocked":
            parts.append(ev["text"])
    return "\n".join(parts)


def _both_paths(question: str, patches) -> tuple[object, list[dict]]:
    """同一问题、同一套打桩，分别跑批量与流式。"""
    with ExitStack() as stack:
        for p in patches():
            stack.enter_context(p)
        batch = loop.run_turn(question, client=_NeverCalledClient())
    with ExitStack() as stack:
        for p in patches():
            stack.enter_context(p)
        events = list(loop.stream_turn(question, client=_NeverCalledClient()))
    return batch, events


class PreferredToolWithoutEvidenceTest(unittest.TestCase):
    """偏好工具一条证据都没返回 → 两条路径都必须走缺据声明。

    这就是重构修掉的那个漏改：流式此前没有这段兜底。
    """

    QUESTION = "最大生命能从哪些渠道获得"

    def _patches(self):
        empty = ToolResult(ok=True, data={"channels": []}, citations=[], meta={})
        return [
            patch.object(loop, "get_config", lambda: _FakeConfig()),
            patch.object(loop, "_persist", lambda *a, **k: None),
            patch.object(loop, "_record_gaps", lambda *a, **k: None),
            patch.object(loop, "_precision_tool_route", lambda _q: ""),
            patch.object(loop, "_preferred_tool_route", lambda _q: "stat_sources"),
            patch.object(loop, "_invoke_preferred_tool", lambda *a, **k: empty),
            patch.object(
                loop.REGISTRY, "call",
                lambda *_a, **_k: ToolResult(
                    ok=True,
                    data={"sufficient": False, "reason": "BM25 依据不足",
                          "missing_terms": ["最大生命"]},
                    citations=[], meta={"channels": ["bm25"], "degraded": False},
                ),
            ),
        ]

    def test_两条路径都判缺据且理由一致(self) -> None:
        batch, events = _both_paths(self.QUESTION, self._patches)
        self.assertTrue(batch.evidence_gap, "批量应判缺据")
        self.assertIn("stat_sources", batch.answer)

        done = [e for e in events if e.get("type") == "done"]
        self.assertEqual(1, len(done))
        self.assertTrue(done[0]["evidence_gap"], "流式也必须判缺据——这是曾经漏改的那段")
        self.assertEqual(batch.answer, _answer_of(events),
                         "同一问题两条路径的缺据声明必须逐字一致")

    def test_流式没有把零证据答案推给闸门(self) -> None:
        _, events = _both_paths(self.QUESTION, self._patches)
        self.assertFalse([e for e in events if e.get("type") == "blocked"],
                         "零证据时不该出现逐句拦截——那比一句查不到更糟")


class ModelInvokedToolWithoutEvidenceTest(unittest.TestCase):
    """代码推不出参数、只能让模型自己调工具，而它调完仍无证据 → 生成之后兜底。

    这一支必须留在 `_terminal_gap`：不给模型调工具的机会就直接拒答，会把本来能答的
    问题拒掉（错误拒答），而错误拒答与漏出要成对压制。
    """

    QUESTION = "法术压制能从哪些渠道获得"

    def _patches(self):
        return [
            patch.object(loop, "get_config", lambda: _FakeConfig()),
            patch.object(loop, "_persist", lambda *a, **k: None),
            patch.object(loop, "_record_gaps", lambda *a, **k: None),
            patch.object(loop, "_precision_tool_route", lambda _q: ""),
            patch.object(loop, "_preferred_tool_route", lambda _q: "stat_sources"),
            # None = 代码推不出参数，要靠模型自己调
            patch.object(loop, "_invoke_preferred_tool", lambda *a, **k: None),
            patch.object(loop, "build_system_prompt", lambda *a, **k: "sys"),
            patch.object(
                loop.REGISTRY, "call",
                lambda *_a, **_k: ToolResult(
                    ok=True,
                    data={"sufficient": False, "reason": "BM25 依据不足",
                          "missing_terms": ["法术压制"]},
                    citations=[], meta={"channels": ["bm25"], "degraded": False},
                ),
            ),
        ]

    def test_两条路径都在生成之后判缺据(self) -> None:
        answer = "法术压制可以从若干渠道获得。"
        with ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            batch = loop.run_turn(
                self.QUESTION,
                client=_ChatClient([LLMResponse(content=answer, model="fake")]),
            )
        with ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            events = list(loop.stream_turn(
                self.QUESTION,
                client=_StreamClient([[StreamEvent(kind="content", text=answer)]]),
            ))

        self.assertTrue(batch.evidence_gap)
        self.assertIn("stat_sources", batch.answer)
        done = [e for e in events if e.get("type") == "done"][0]
        self.assertTrue(done["evidence_gap"], "流式同样要兜住")
        self.assertNotIn(answer, _answer_of(events),
                         "无据正文不得下发，缺据声明取代它")


class RetrievalUnavailableTest(unittest.TestCase):
    """检索不可用：两条路径此前是两套措辞，现在同一句。"""

    def _patches(self):
        return [
            patch.object(loop, "get_config", lambda: _FakeConfig()),
            patch.object(loop, "_persist", lambda *a, **k: None),
            patch.object(loop, "_record_gaps", lambda *a, **k: None),
            patch.object(
                loop.REGISTRY, "call",
                lambda *_a, **_k: ToolResult(ok=False, error="FTS5 索引损坏"),
            ),
        ]

    def test_措辞一致且都判缺据(self) -> None:
        batch, events = _both_paths("猎首有什么效果", self._patches)
        self.assertTrue(batch.evidence_gap)
        self.assertIn("FTS5 索引损坏", batch.answer)
        self.assertEqual(batch.answer, _answer_of(events))


class OutOfScopeTest(unittest.TestCase):
    """POE2 越界：判据在原始问题串上，两条路径同一套。"""

    def _patches(self):
        return [
            patch.object(loop, "get_config", lambda: _FakeConfig()),
            patch.object(loop, "_persist", lambda *a, **k: None),
            patch.object(loop, "_record_gaps", lambda *a, **k: None),
            patch.object(
                loop.REGISTRY, "call",
                lambda *_a, **_k: (_ for _ in ()).throw(
                    AssertionError("越界问题不该消耗检索")
                ),
            ),
        ]

    def test_两条路径都在检索之前拦下(self) -> None:
        batch, events = _both_paths("poe2 的升华怎么改了", self._patches)
        self.assertTrue(batch.evidence_gap)
        self.assertEqual(batch.answer, _answer_of(events))
        self.assertEqual("poe2", batch.retrieval_meta.get("out_of_scope"))


class CoverageAnswerTest(unittest.TestCase):
    """覆盖声明是「已答完」而不是「缺据」——两条路径的这个区分必须一致。"""

    def _patches(self):
        cov = ToolResult(
            ok=True,
            data={"manifest": "已采集：唯一装备、词缀\n未采集：经济价格"},
            citations=[], meta={},
        )
        return [
            patch.object(loop, "get_config", lambda: _FakeConfig()),
            patch.object(loop, "_persist", lambda *a, **k: None),
            patch.object(loop, "_record_gaps", lambda *a, **k: None),
            patch.object(loop, "_precision_tool_route", lambda _q: ""),
            patch.object(loop, "_preferred_tool_route", lambda _q: "kb_coverage"),
            patch.object(loop, "_invoke_preferred_tool", lambda *a, **k: cov),
            patch.object(loop, "_coverage_answer", lambda _r: "本库覆盖：唯一装备、词缀。"),
            patch.object(
                loop.REGISTRY, "call",
                lambda *_a, **_k: ToolResult(
                    ok=True,
                    data={"sufficient": False, "reason": "元信息问题",
                          "missing_terms": []},
                    citations=[], meta={"channels": ["bm25"], "degraded": False},
                ),
            ),
        ]

    def test_都不判缺据且文本一致(self) -> None:
        batch, events = _both_paths("你的知识库覆盖了哪些领域", self._patches)
        self.assertFalse(batch.evidence_gap, "覆盖声明是答完了，不是无据")
        self.assertEqual("本库覆盖：唯一装备、词缀。", batch.answer)

        done = [e for e in events if e.get("type") == "done"][0]
        self.assertFalse(done["evidence_gap"])
        self.assertEqual(batch.answer, _answer_of(events))
        kinds = [e["type"] for e in events]
        self.assertIn("sentence", kinds)
        self.assertNotIn("gap", kinds, "覆盖声明不该发 gap 事件")


class EconomyAnswerTest(unittest.TestCase):
    """实时价格走确定性答案：批量/流式逐字一致，且完全不调用模型。"""

    ANSWER = (
        "### Headhunter · Allflame 价格快照\n\n"
        "- poe.watch 均值：**100 混沌石**\n\n"
        "> 数据源：poe.watch；联盟：Allflame；抓取时间：2026-09-21T04:00:00Z。"
    )

    def _patches(self):
        def call(name, _arguments):
            if name == "kb_search":
                return ToolResult(
                    ok=True,
                    data={"sufficient": False, "reason": "动态价格不在静态卡片", "missing_terms": []},
                    citations=[],
                    meta={"channels": ["bm25"], "degraded": False},
                )
            if name == "economy_price_lookup":
                return ToolResult(
                    ok=True,
                    data={"answer": self.ANSWER, "found": True},
                    citations=[{
                        "chunk_id": "economy:Allflame:accessory:1:2026-09-21T04:00:00Z",
                        "entity_id": None,
                        "title": "Headhunter price",
                        "url": "https://api.poe.watch",
                        "source": "poe.watch",
                        "source_priority": 5,
                        "can_support_facts": True,
                        "text": self.ANSWER,
                        "channel": "live_economy",
                    }],
                    meta={"channel": "live_economy"},
                )
            raise AssertionError(name)

        return [
            patch.object(loop, "get_config", lambda: _FakeConfig()),
            patch.object(loop, "_persist", lambda *a, **k: None),
            patch.object(loop, "_record_gaps", lambda *a, **k: None),
            patch.object(loop.REGISTRY, "call", call),
        ]

    def test_两条路径都直接返回同一价格快照(self) -> None:
        batch, events = _both_paths("猎首现在值多少混沌石", self._patches)
        self.assertFalse(batch.evidence_gap)
        self.assertEqual(self.ANSWER, batch.answer)
        self.assertEqual(self.ANSWER, _answer_of(events))
        self.assertEqual(
            ["kb_search", "economy_price_lookup"],
            [call["tool"] for call in batch.tool_calls],
        )


class MaterialCostAnswerTest(unittest.TestCase):
    """明确材料清单也由代码计算并直出，两条传输路径不得经过模型改写。"""

    ANSWER = (
        "### Allflame 材料清单成本快照\n\n"
        "**均值总成本：840.5 混沌石**\n\n"
        "> 数据源：poe.watch；联盟：Allflame；最早抓取时间：2026-09-21T04:00:00Z。"
    )

    def _patches(self):
        def call(name, arguments):
            if name == "kb_search":
                return ToolResult(
                    ok=True,
                    data={"sufficient": False, "reason": "动态成本不在静态卡片", "missing_terms": []},
                    citations=[],
                    meta={"channels": ["bm25"], "degraded": False},
                )
            if name == "economy_material_cost":
                args = json.loads(arguments)
                self.assertEqual(2, len(args["materials"]))
                return ToolResult(
                    ok=True,
                    data={"answer": self.ANSWER, "calculable": True},
                    citations=[{
                        "chunk_id": "economy-cost:Allflame:abc",
                        "entity_id": None,
                        "title": "Allflame material cost",
                        "url": "https://api.poe.watch",
                        "source": "poe.watch",
                        "source_priority": 5,
                        "can_support_facts": True,
                        "text": self.ANSWER,
                        "channel": "live_economy",
                    }],
                    meta={"channel": "live_economy"},
                )
            raise AssertionError(name)

        return [
            patch.object(loop, "get_config", lambda: _FakeConfig()),
            patch.object(loop, "_persist", lambda *a, **k: None),
            patch.object(loop, "_record_gaps", lambda *a, **k: None),
            patch.object(loop.REGISTRY, "call", call),
        ]

    def test_两条路径逐字一致且不调用模型(self) -> None:
        question = "2 个神圣石和 100 个混沌石一共多少钱"
        batch, events = _both_paths(question, self._patches)
        self.assertFalse(batch.evidence_gap)
        self.assertEqual(self.ANSWER, batch.answer)
        self.assertEqual(self.ANSWER, _answer_of(events))
        self.assertEqual(
            ["kb_search", "economy_material_cost"],
            [call["tool"] for call in batch.tool_calls],
        )


class CraftOperationAnswerTest(unittest.TestCase):
    """基础通货状态转移由代码直接输出，批量与流式不得交给模型扩写。"""

    ANSWER = (
        "### 崇高石状态转移\n\n"
        "**结论：已知约束兼容**\n\n"
        "> 只证明稀有度与前后缀名额；不提供具体结果、概率、成本或路线。"
    )

    def _patches(self):
        def call(name, arguments):
            if name == "kb_search":
                return ToolResult(
                    ok=True,
                    data={"sufficient": False, "reason": "状态转移不在静态卡片", "missing_terms": []},
                    citations=[],
                    meta={"channels": ["bm25"], "degraded": False},
                )
            if name == "craft_operation_state":
                args = json.loads(arguments)
                self.assertEqual("崇高石", args["operation"])
                self.assertEqual(2, args["prefix_count"])
                return ToolResult(
                    ok=True,
                    data={"answer": self.ANSWER, "found": True, "verdict": "compatible"},
                    citations=[{
                        "chunk_id": "craft_operation:exalted",
                        "entity_id": None,
                        "title": "崇高石状态转移",
                        "url": "https://poedb.tw/cn/Exalted_Orb",
                        "source": "POEDB",
                        "source_priority": 1,
                        "can_support_facts": True,
                        "text": self.ANSWER,
                        "channel": "structured",
                    }],
                    meta={"channel": "structured"},
                )
            raise AssertionError(name)

        return [
            patch.object(loop, "get_config", lambda: _FakeConfig()),
            patch.object(loop, "_persist", lambda *a, **k: None),
            patch.object(loop, "_record_gaps", lambda *a, **k: None),
            patch.object(loop.REGISTRY, "call", call),
        ]

    def test_两条路径直接返回同一状态转移(self) -> None:
        question = "稀有装备 2 条前缀 3 条后缀还能用崇高石吗"
        batch, events = _both_paths(question, self._patches)
        self.assertFalse(batch.evidence_gap)
        self.assertEqual(self.ANSWER, batch.answer)
        self.assertEqual(self.ANSWER, _answer_of(events))
        self.assertEqual(
            ["kb_search", "craft_operation_state"],
            [call["tool"] for call in batch.tool_calls],
        )


class EssenceOperationAnswerTest(unittest.TestCase):
    """精华状态转移由代码直接输出，批量与流式不得交给模型扩写。"""

    ANSWER = (
        "### 憎恨之破空精华状态转移\n\n"
        "**结论：已知约束兼容**\n\n"
        "> 只证明稀有度、物等与类别前提；不提供额外词缀、概率、成本或路线。"
    )

    def _patches(self):
        def call(name, arguments):
            if name == "kb_search":
                return ToolResult(
                    ok=True,
                    data={"sufficient": False, "reason": "状态转移不在静态卡片", "missing_terms": []},
                    citations=[],
                    meta={"channels": ["bm25"], "degraded": False},
                )
            if name == "essence_operation_state":
                args = json.loads(arguments)
                self.assertEqual("憎恨之破空精华", args["essence"])
                self.assertEqual(84, args["item_level"])
                self.assertEqual("项链", args["item_class"])
                return ToolResult(
                    ok=True,
                    data={"answer": self.ANSWER, "found": True, "verdict": "compatible"},
                    citations=[{
                        "chunk_id": "essence_operation:hatred7",
                        "entity_id": None,
                        "title": "憎恨之破空精华状态转移",
                        "url": "https://poedb.tw/cn/Essence",
                        "source": "POEDB",
                        "source_priority": 1,
                        "can_support_facts": True,
                        "text": self.ANSWER,
                        "channel": "structured",
                    }],
                    meta={"channel": "structured"},
                )
            raise AssertionError(name)

        return [
            patch.object(loop, "get_config", lambda: _FakeConfig()),
            patch.object(loop, "_persist", lambda *a, **k: None),
            patch.object(loop, "_record_gaps", lambda *a, **k: None),
            patch.object(loop.REGISTRY, "call", call),
        ]

    def test_两条路径直接返回同一精华状态转移(self) -> None:
        question = "物等84的稀有项链能用憎恨之破空精华吗"
        batch, events = _both_paths(question, self._patches)
        self.assertFalse(batch.evidence_gap)
        self.assertEqual(self.ANSWER, batch.answer)
        self.assertEqual(self.ANSWER, _answer_of(events))
        self.assertEqual(
            ["kb_search", "essence_operation_state"],
            [call["tool"] for call in batch.tool_calls],
        )


class HarvestReforgeAnswerTest(unittest.TestCase):
    """Harvest 重铸状态由代码直接输出，批量与流式不得交给模型扩写。"""

    ANSWER = (
        "### Harvest 重铸状态转移\n\n"
        "**结论：已知约束兼容**\n\n"
        "> 保底火焰标签；具体词缀、概率、市场成本与路线未知。"
    )

    def _patches(self):
        def call(name, arguments):
            if name == "kb_search":
                return ToolResult(
                    ok=True,
                    data={"sufficient": False, "reason": "状态转移不在静态卡片", "missing_terms": []},
                    citations=[],
                    meta={"channels": ["bm25"], "degraded": False},
                )
            if name == "harvest_reforge_state":
                args = json.loads(arguments)
                self.assertEqual("火焰", args["operation"])
                self.assertEqual("稀有", args["rarity"])
                return ToolResult(
                    ok=True,
                    data={"answer": self.ANSWER, "found": True, "verdict": "compatible"},
                    citations=[{
                        "chunk_id": "harvest_operation:fire",
                        "entity_id": None,
                        "title": "Harvest 火焰重铸",
                        "url": "https://poedb.tw/cn/Horticrafting",
                        "source": "POEDB",
                        "source_priority": 1,
                        "can_support_facts": True,
                        "text": self.ANSWER,
                        "channel": "structured",
                    }],
                    meta={"channel": "structured"},
                )
            raise AssertionError(name)

        return [
            patch.object(loop, "get_config", lambda: _FakeConfig()),
            patch.object(loop, "_persist", lambda *a, **k: None),
            patch.object(loop, "_record_gaps", lambda *a, **k: None),
            patch.object(loop.REGISTRY, "call", call),
        ]

    def test_两条路径直接返回同一Harvest状态转移(self) -> None:
        question = "收获火焰重铸能用于稀有装备吗"
        batch, events = _both_paths(question, self._patches)
        self.assertFalse(batch.evidence_gap)
        self.assertEqual(self.ANSWER, batch.answer)
        self.assertEqual(self.ANSWER, _answer_of(events))
        self.assertEqual(
            ["kb_search", "harvest_reforge_state"],
            [call["tool"] for call in batch.tool_calls],
        )


class HarvestModifierAnswerTest(unittest.TestCase):
    """Harvest 定向词缀状态由代码直出，批量与流式不得改写近似位阶。"""

    ANSWER = (
        "### Harvest 定向词缀状态转移\n\n"
        "**结论：已知约束兼容**\n\n"
        "> 火焰抗性转换为近似位阶的冰霜抗性；精确位阶未知。"
    )

    def _patches(self):
        def call(name, arguments):
            if name == "kb_search":
                return ToolResult(
                    ok=True,
                    data={"sufficient": False, "reason": "状态转移不在静态卡片", "missing_terms": []},
                    citations=[],
                    meta={"channels": ["bm25"], "degraded": False},
                )
            if name == "harvest_modifier_state":
                args = json.loads(arguments)
                self.assertEqual("火焰抗性转冰霜抗性", args["operation"])
                return ToolResult(
                    ok=True,
                    data={"answer": self.ANSWER, "found": True, "verdict": "unknown"},
                    citations=[{
                        "chunk_id": "harvest_modifier_operation:fire-to-cold-resistance",
                        "entity_id": None,
                        "title": "Harvest 火焰抗性转冰霜抗性",
                        "url": "https://poedb.tw/cn/Horticrafting",
                        "source": "POEDB",
                        "source_priority": 1,
                        "can_support_facts": True,
                        "text": self.ANSWER,
                        "channel": "structured",
                    }],
                    meta={"channel": "structured"},
                )
            raise AssertionError(name)

        return [
            patch.object(loop, "get_config", lambda: _FakeConfig()),
            patch.object(loop, "_persist", lambda *a, **k: None),
            patch.object(loop, "_record_gaps", lambda *a, **k: None),
            patch.object(loop.REGISTRY, "call", call),
        ]

    def test_两条路径直接返回同一Harvest定向词缀结果(self) -> None:
        question = "收获能把火焰抗性转成冰霜抗性吗"
        batch, events = _both_paths(question, self._patches)
        self.assertFalse(batch.evidence_gap)
        self.assertEqual(self.ANSWER, batch.answer)
        self.assertEqual(self.ANSWER, _answer_of(events))
        self.assertEqual(
            ["kb_search", "harvest_modifier_state"],
            [call["tool"] for call in batch.tool_calls],
        )


class PartialDimensionDisclosureTest(unittest.TestCase):
    """复合问题继续回答有据部分，同时由代码保证披露无据部分。"""

    QUESTION = "猎首有哪些词缀，值多少钱"
    NOTE = (
        "**本题仍有未覆盖部分**：市场价格的数据（未采集）。"
        "上文仅回答本库有证据的部分；未覆盖部分不作推断；"
        "查证入口：https://api.poe.watch。"
    )

    class _Verdict:
        is_gap = False
        is_partial = True

        @classmethod
        def partial_note(cls) -> str:
            return PartialDimensionDisclosureTest.NOTE

        @staticmethod
        def missing_points() -> list[str]:
            return ["市场价格的数据"]

        @staticmethod
        def category_label() -> str:
            return "not_collected/economy_price"

    def _patches(self, recorded: list[tuple[list[str], str]]):
        citation = {
            "chunk_id": "unique_item:Headhunter#one",
            "entity_id": "unique_item:Headhunter",
            "title": "猎首（Headhunter）",
            "url": "https://poedb.tw/cn/Headhunter",
            "source": "POEDB",
            "source_priority": 1,
            "can_support_facts": True,
            "text": "猎首提供 +(40-55) 力量",
            "channel": "alias",
        }
        return [
            patch.object(loop, "get_config", lambda: _FakeConfig()),
            patch.object(loop, "_persist", lambda *a, **k: None),
            patch.object(loop, "_record_binding_gaps", lambda *a, **k: None),
            patch.object(
                loop,
                "_record_gaps",
                lambda _q, points, *, category="", **_k: recorded.append(
                    (list(points), category)
                ),
            ),
            patch.object(loop, "_precision_tool_route", lambda _q: ""),
            patch.object(loop, "_preferred_tool_route", lambda _q: ""),
            patch.object(loop, "_dimension_verdict", lambda *_a, **_k: self._Verdict()),
            patch.object(loop, "build_system_prompt", lambda *a, **k: "sys"),
            patch.object(loop, "_prepare_tools", lambda: []),
            patch.object(loop, "_domain_terms", lambda: ()),
            patch.object(
                loop.REGISTRY,
                "call",
                lambda *_a, **_k: ToolResult(
                    ok=True,
                    data={"sufficient": True, "reason": "实体精确命中"},
                    citations=[citation],
                    meta={"channels": ["alias"], "degraded": False},
                ),
            ),
        ]

    def test_批量与流式都保留答案并附同一缺口声明(self) -> None:
        answer = "猎首提供 +(40-55) 力量[[E1]]。"
        recorded: list[tuple[list[str], str]] = []
        with ExitStack() as stack:
            for p in self._patches(recorded):
                stack.enter_context(p)
            batch = loop.run_turn(
                self.QUESTION,
                client=_ChatClient([LLMResponse(content=answer, model="fake")]),
            )
        with ExitStack() as stack:
            for p in self._patches(recorded):
                stack.enter_context(p)
            events = list(loop.stream_turn(
                self.QUESTION,
                client=_StreamClient([[StreamEvent(kind="content", text=answer)]]),
            ))

        self.assertFalse(batch.evidence_gap)
        self.assertIn(answer, batch.answer)
        self.assertIn(self.NOTE, batch.answer)
        self.assertTrue(batch.retrieval_meta["partial_dimension_gap"])
        notes = [e["text"] for e in events if e.get("type") == "note"]
        self.assertEqual([self.NOTE], notes)
        self.assertIn(answer, _answer_of(events))
        self.assertEqual(
            [(["市场价格的数据"], "not_collected/economy_price")] * 2,
            recorded,
        )


class SharedPipelineTest(unittest.TestCase):
    """结构约束：两条路径都必须经过共享部件，不允许谁再自己抄一份。

    这条是给未来的改动看的——行为测试只能发现已知的分叉形态，
    而分叉的根因是「又在某一侧写了一遍」。
    """

    def test_两条路径都调用共享的准备与收尾(self) -> None:
        import inspect

        for fn in (loop.run_turn, loop.stream_turn):
            src = inspect.getsource(fn)
            with self.subTest(fn=fn.__name__):
                self.assertIn("_prepare_turn(", src, "准备阶段必须共用")
                self.assertIn("_terminal_gap(", src, "确定性拒答必须共用")
                self.assertIn("_handle_tool_call(", src, "单次工具调用必须共用")
                # 分叉都是从「在这一侧重新判一次」开始的
                self.assertNotIn("_precision_tool_route(", src)
                self.assertNotIn("_apply_dimension_gate(", src)
                self.assertNotIn("poe2_marker(", src)


if __name__ == "__main__":
    unittest.main()


class DimensionGateSingleImplTest(unittest.TestCase):
    """维度闸门只能有**一份**实现，评测必须用线上那一份。

    第四十七轮 review 抓到的形态：`_apply_dimension_gate` 是死代码、
    `_prepare_turn` 内联了一份等价实现、`scripts/eval_run.py` 又自己调
    `dimension_gap` 并在外面套了 `if sufficient:`（第十八轮之前的语义）。
    **三份实现、两种行为，而注释声称是同一个函数。**
    """

    def test_prepare_turn_调用共享实现而不是自己内联(self) -> None:
        import inspect

        src = inspect.getsource(loop._prepare_turn)
        self.assertIn("_apply_dimension_gate(", src, "必须调共享实现")
        # 内联的标志：自己设这几个字段
        self.assertNotIn('data["missing_terms"] =', src, "不许再内联一份改判")

    def test_评测走的是线上同一个函数(self) -> None:
        """评测量的必须是线上真正生效的那条路径，否则指标与线上分叉。

        只看 `run_case` 的**代码行**：注释里出现 `if sufficient:` 是在讲这段历史，
        整文件 grep 会把讲述历史的那句话本身判成违规。
        """
        import ast

        path = Path(__file__).resolve().parents[2] / "scripts" / "eval_run.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        run_case = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "run_case"
        )
        code = "\n".join(
            line for line in ast.unparse(run_case).splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertIn("_apply_dimension_gate(", code, "评测必须调线上那一份")
        self.assertNotIn("if sufficient", code,
                         "外面套 if sufficient 就退回了第十八轮之前的语义")

    def test_无条件问覆盖层(self) -> None:
        """「这一维有没有数据」与「这次 BM25 有没有命中」是两个问题。
        第十八轮实测：只在充分时问，偏好工具那条路上它从没被咨询过。"""
        import inspect

        src = inspect.getsource(loop._apply_dimension_gate)
        body = src.split('"""')[-1]
        self.assertNotIn("sufficient", body.split("verdict =")[0],
                         "改判前不许先看 sufficient")
