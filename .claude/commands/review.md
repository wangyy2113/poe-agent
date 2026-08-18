---
description: 全局 review：找出补丁式改动、成对点漏改、死代码与文档漂移
---

按 `.claude/ENGINEERING.md` 做一次自顶向下的 review。**先读规约，再看代码**，
输出「该重构什么 / 该删什么 / 哪里漏改了」，不要只报表面问题。

## 1. 先看规模与增长点

```bash
wc -l orchestrator/*.py retrieval/*.py kb/*.py tools/impl/*.py core/*.py agents/*.py | sort -rn | head -20
git log --oneline -20
git diff --stat main...HEAD 2>/dev/null
```

`orchestrator/loop.py` 与 `kb/pipeline.py` 是本仓最容易长胖的两处。**行数本身不是问题，
一个函数同时做「取数 / 判定 / 生成文本」才是问题。**

## 2. 逐项对照补丁症状（规约第 3 条）

- 同一概念在多处各写一份判据 → 尤其查批量 vs 流式、提示词 vs 闸门
- 正则白名单在加语义重叠的新项 → 正解通常是正向匹配原字段
- 按用例名 / 问题串特判的 `if` → 评测是度量，不是分支条件
- 排序层加权/限流/调系数 → 先怀疑索引切分粒度

```bash
grep -rn "^_[A-Z_]*_RE = re.compile" orchestrator/ retrieval/ kb/ | wc -l   # 正则判据总数，只增不减是信号
grep -rn "TODO\|FIXME\|暂时\|先这样\|后面再" --include=*.py . | grep -v .venv
```

## 3. 查死代码

```bash
# 定义了但全仓无引用的模块级函数（含测试与脚本一并搜，避免误删）
.venv/bin/python - <<'PY'
import ast, pathlib, collections
defs, uses = {}, collections.Counter()
for p in pathlib.Path('.').rglob('*.py'):
    if any(x in p.parts for x in ('.venv', 'semantic-review')): continue
    t = ast.parse(p.read_text(encoding='utf-8'))
    for n in ast.walk(t):
        if isinstance(n, ast.FunctionDef) and n.col_offset == 0:
            defs.setdefault(n.name, []).append(f"{p}:{n.lineno}")
        if isinstance(n, ast.Name): uses[n.id] += 1
        if isinstance(n, ast.Attribute): uses[n.attr] += 1
for name, where in sorted(defs.items()):
    if uses[name] == 0:          # 定义本身不产生 Name 节点，所以 ==0 才是「全仓无引用」
        print(f"疑似无引用：{name}  {where}")
PY
```

阈值只能是 `== 0`。写成 `<= len(where)` 会把「只被调用一次」的函数全部报出来——
首次跑出 250 条噪声就是这么来的（规约第 6 条：先确认测量本身是对的）。
即使 `== 0` 也仍有假阳性：装饰器注册的路由、`getattr` 动态调用、只在 f-string 里出现的名字。

判定无用要看三处：应用代码、`scripts/`、`tests/`。只被测试引用的函数**也是死代码**
（测试在测一个没人用的东西），但删之前确认它不是被 spec 要求的能力。

## 4. 查成对点漏改（规约第 5 条）

对照规约第 5 条的表逐行确认。最容易漏的三处，直接跑：

```bash
.venv/bin/python -m unittest tests.unit.test_streaming_gate    # 批量与流式一致
.venv/bin/python scripts/check_doc_facts.py                    # 文档数字
.venv/bin/python scripts/check_spec_refs.py                    # spec 交叉引用
```

提示词与闸门的矛盾（「一边要求模型说、一边拦掉它」）没有自动检查——
读 `agents/knowledge.py` 里每条「必须说明 / 必须写出」的要求，问自己：**这句话说出来能绑定到证据吗？**
不能，就该落到代码生成层。

## 5. 收口

按重构价值排序输出，每项写清：症状、正确的层在哪、动了会影响哪些成对点、怎么验证没丢能力。
**不要在 review 里顺手改代码**——先出结论，改动单独走规约第 7 条的收口流程。
