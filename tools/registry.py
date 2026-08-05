"""工具注册与 JSON Schema 导出（任务 3.2）。

工具的返回值统一包成 `ToolResult`，其中 `citations` 是**结构化引用**而不是
拼进文本里的字符串。理由：引用要参与出口的绑定校验，必须能被程序读取。
让模型自己在正文里写来源，等于把校验交给模型自觉。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    """统一包封。`ok=False` 时上层应降级并在答案中标注该能力不可用（需求 4.3）。"""

    ok: bool
    data: Any = None
    #: 结构化引用，元素形如 {chunk_id, entity_id, title, url, source, source_priority, text}
    citations: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    elapsed_ms: int = 0
    #: 工具自报的补充信息（生效检索通道、是否降级等），进轨迹便于复盘
    meta: dict[str, Any] = field(default_factory=dict)

    def for_model(self) -> str:
        """喂给 LLM 的紧凑表示。

        故意**不把 url 塞进正文**：模型看到 URL 就会倾向于自己编造引用格式，
        而引用应由代码在出口统一装配。模型只需要知道每条证据的编号与内容。
        """
        if not self.ok:
            return json.dumps({"ok": False, "error": self.error}, ensure_ascii=False)
        payload: dict[str, Any] = {"ok": True}
        if self.data is not None:
            payload["data"] = self.data
        if self.meta:
            payload["meta"] = self.meta
        return json.dumps(payload, ensure_ascii=False)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., ToolResult]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
    ) -> Callable[[Callable[..., ToolResult]], Callable[..., ToolResult]]:
        def deco(fn: Callable[..., ToolResult]) -> Callable[..., ToolResult]:
            if name in self._tools:
                raise ValueError(f"工具重名：{name}")
            self._tools[name] = ToolSpec(name, description, parameters, fn)
            return fn

        return deco

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self, allow: list[str] | None = None) -> list[dict[str, Any]]:
        """导出给 LLM 的 tools 参数。`allow` 限定 Agent 的可用工具集。"""
        names = allow if allow is not None else self.names()
        return [self._tools[n].schema() for n in names if n in self._tools]

    def call(self, name: str, raw_arguments: str) -> ToolResult:
        """按名调用。参数解析失败与工具异常都转成 `ok=False`，不抛给主循环。

        主循环需要把失败结果回传给模型让它换个问法重试，抛异常会中断整轮。
        """
        started = time.monotonic()
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(
                ok=False,
                error=f"未注册的工具 {name!r}，可用：{', '.join(self.names())}",
            )
        try:
            args = json.loads(raw_arguments) if raw_arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return ToolResult(ok=False, error=f"参数不是合法 JSON：{exc}")
        if not isinstance(args, dict):
            return ToolResult(ok=False, error="参数必须是 JSON 对象")

        try:
            result = spec.handler(**args)
        except TypeError as exc:
            return ToolResult(ok=False, error=f"参数不匹配：{exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("工具 %s 执行失败", name)
            result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result


#: 进程内全局注册表。导入 tools.impl 即完成注册。
REGISTRY = ToolRegistry()
