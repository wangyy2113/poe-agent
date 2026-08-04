#!/usr/bin/env python3
"""校验 spec 文档间的需求交叉引用是否有效。

解析 requirements.md 中每个需求的验收标准条目数，然后检查 design.md、
tasks.md、steering 文档里所有 "需求 X.Y" 引用是否都指向真实存在的条目。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQ = ROOT / ".kiro/specs/poe1-agent/requirements.md"
TARGETS = [
    ROOT / ".kiro/specs/poe1-agent/design.md",
    ROOT / ".kiro/specs/poe1-agent/tasks.md",
    ROOT / ".kiro/steering/project-conventions.md",
]


def parse_criteria(text: str) -> dict[tuple[int, int], str]:
    """返回 {(需求编号, 条目编号): 条目正文}。"""
    criteria: dict[tuple[int, int], str] = {}
    current: int | None = None
    in_criteria = False
    for line in text.split("\n"):
        heading = re.match(r"^### 需求 (\d+)[：:]", line)
        if heading:
            current = int(heading.group(1))
            in_criteria = False
            continue
        if line.startswith("#### 验收标准"):
            in_criteria = True
            continue
        # 遇到二级标题说明离开了需求列表区域
        if line.startswith("## ") and not line.startswith("### "):
            current, in_criteria = None, False
            continue
        if current is not None and in_criteria:
            item = re.match(r"^(\d+)\. (.*)$", line)
            if item:
                criteria[(current, int(item.group(1)))] = item.group(2)
    return criteria


def parse_requirement_counts(criteria: dict[tuple[int, int], str]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for req_no, item_no in criteria:
        counts[req_no] = max(counts.get(req_no, 0), item_no)
    return counts


def collect_refs(text: str) -> list[tuple[int, int, int, str]]:
    """提取 (行号, 需求号, 条目号, 上下文)。"""
    refs = []
    for lineno, line in enumerate(text.split("\n"), 1):
        # 行内引用，如「（需求 2.5）」
        for a, b in re.findall(r"需求[：:]?\s*(\d+)\.(\d+)", line):
            refs.append((lineno, int(a), int(b), line.strip()[:70]))
        # 任务尾注，如「_需求: 1.1, 1.3, 15.4_」
        tail = re.search(r"_需求:\s*([^_]+)_", line)
        if tail:
            for token in tail.group(1).split(","):
                m = re.fullmatch(r"(\d+)\.(\d+)", token.strip())
                if m:
                    refs.append(
                        (lineno, int(m.group(1)), int(m.group(2)), token.strip())
                    )
    return refs


def main() -> int:
    verbose = "--dump" in sys.argv
    criteria = parse_criteria(REQ.read_text(encoding="utf-8"))
    counts = parse_requirement_counts(criteria)
    print("requirements.md 各需求验收标准条目数：")
    print("  " + ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())))

    problems: list[str] = []
    seen: set[tuple[str, int, int, int]] = set()
    for path in TARGETS:
        rows = []
        for lineno, req_no, item_no, ctx in collect_refs(
            path.read_text(encoding="utf-8")
        ):
            key = (path.name, lineno, req_no, item_no)
            if key in seen:
                continue
            seen.add(key)
            if req_no not in counts:
                problems.append(f"  {path.name}:{lineno}  需求 {req_no} 不存在\n      {ctx}")
            elif not 1 <= item_no <= counts[req_no]:
                problems.append(
                    f"  {path.name}:{lineno}  需求 {req_no}.{item_no} 越界"
                    f"（该需求仅 {counts[req_no]} 条）\n      {ctx}"
                )
            else:
                rows.append((lineno, req_no, item_no, ctx))
        if verbose and rows:
            print(f"\n=== {path.name} 引用指向核对 ===")
            for lineno, req_no, item_no, ctx in rows:
                target = criteria[(req_no, item_no)]
                print(f"  L{lineno} 引用处: {ctx}")
                print(f"        需求 {req_no}.{item_no} 实际内容: {target[:100]}")

    print()
    if problems:
        print(f"发现 {len(problems)} 处失效引用：")
        print("\n".join(problems))
        return 1
    print("所有交叉引用均在有效范围内")
    return 0


if __name__ == "__main__":
    sys.exit(main())
