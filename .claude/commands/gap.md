---
description: 看缺口台账与覆盖现状，判断下一步该补哪类数据
---

按频次排序的缺口台账就是采集优先级（需求 2.12）。看这三处：

```bash
# 1. 台账：只有 retrieval_gap / tool_gap / ingest_gap 算采集待办
.venv/bin/python -c "
import sqlite3; c=sqlite3.connect('data/poe.db'); c.row_factory=sqlite3.Row
print('按来源分布:', {r[0]: r[1] for r in c.execute('select coalesce(kind,\"retrieval_gap\"), count(*) from knowledge_gap group by 1 order by 2 desc')})
print()
for r in c.execute(\"select question, substr(missing_point,1,70) mp, kind, hit_count from knowledge_gap where kind in ('retrieval_gap','tool_gap','ingest_gap') order by hit_count desc limit 15\"):
    print(f'{r[3]:>3}x [{r[2]}] {r[0][:26]} — {r[1]}')
"

# 2. 覆盖现状：inventory × status 的差集就是缺口
.venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from core import db as dbmod; from core.config import get_config
from kb.coverage import load
conn = dbmod.init_db(get_config().db_path)
for s in sorted(load(conn), key=lambda x: (x.status, -x.item_count)):
    note = f'  缺：{s.spec.partial_note[:60]}' if s.status == 'partial' and s.spec.partial_note else ''
    print(f'{s.status:14s} {s.spec.display_zh[:26]:28s} {s.item_count:>7,}{note}')
"

# 3. 跨来源差异：mod 是已确认冲突，mod_review 只是待人工核对
.venv/bin/python -c "
import sqlite3; c=sqlite3.connect('data/poe.db')
print({r[0]: r[1] for r in c.execute('select kind, count(*) from source_conflict group by 1 order by 2 desc')})
"
```

## 判读要点

- **`binding_block` 与 `binding_block_legacy` 不是采集待办**，别混进优先级。它们是出口闸门
  拦截行为的观测量：`legacy` 是迁移前混进来的噪声（句子原文当 missing_point，
  库里有 156 条），保留只为可追溯。
- **高频条目未必是活的待办：先看 `last_seen`。** 台账是审计日志，根因修好后历史命中数
  仍留在原位。实测排最前的两条（「腰带上的力量词缀一共有几档」因通用问法词被误拒、
  「你的知识库covered哪些领域」走了检索而非覆盖层）根因都已修掉，命中数却仍在榜首。
  拿一条高频项当待办之前，用它的原问题**实跑一次**确认现在还复现。
- **`status` 的四个取值意思不同**：`ingested` 已采集；`partial` 有数据但缺维度，
  必须看 `partial_note` 说缺什么；`harvested` **页面已抓到本地缓存但一条都没入库**
  （缺的是抽取器，不是抓取）；`not_collected` 完全没有。
  `harvested` 通常是性价比最高的下一步——网络那步已经做完了。
- **`mod_review` 不得当作冲突呈现**（需求 15.5）。库里 `kind='mod'` 的已确认冲突目前是
  **0 条**，所以「跨来源冲突并列展示」这条能力无法验收——缺的是人工核对，不是代码。
- 问某个领域为什么答不上来时，先看它是 `harvested` 还是 `not_collected`：
  前者对用户要说「本库未采集该领域」并给权威来源，**不能说成「查不到」**——
  后者会让用户以为游戏里没这东西。
