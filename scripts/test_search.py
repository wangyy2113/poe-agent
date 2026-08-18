#!/usr/bin/env python3
"""检索链路验证：专名查询、描述性查询、以及知识库确定不覆盖的查询。

最后一类最关键——用来验证「无据不答」是否真的生效，而不是编一个答案。

**用例已迁到 `tests/golden/questions.yaml`**，本脚本只是按类别筛选后调用
`scripts/eval_run.py` 的检索侧评测。原先这里内嵌一份 21 条的 CASES，与评测集
是两份数据；两份用例迟早分叉，届时同一个查询在两处得到不同结论，哪个对都说不清。
用例的来历与踩过的坑记在 yaml 的 `lesson` 字段里，不要删。

    python scripts/test_search.py           # 专名 + 描述性 + 拒答类
    python scripts/eval_run.py              # 全部类别与完整指标
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_run import main as eval_main  # noqa: E402

#: 本脚本的历史职责就是这三类：能命中的、描述性能命中的、必须拒答的。
#: 数值类与跨来源类的完整判定需要生成结果，走 eval_run.py。
KINDS = "name,description,gap"


def main() -> int:
    argv = sys.argv[1:]
    if not any(a.startswith("--kind") for a in argv):
        argv = ["--kind", KINDS, *argv]
    sys.argv = [sys.argv[0], *argv]
    return eval_main()


if __name__ == "__main__":
    raise SystemExit(main())
