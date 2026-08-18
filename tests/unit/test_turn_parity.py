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
