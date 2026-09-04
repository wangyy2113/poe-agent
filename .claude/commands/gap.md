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
- **`mod_review` / `unique_review` / `spectre_review` 等 `*_review` 不得当作冲突呈现**
  （需求 15.5）——它们只是待人工核对。**已确认冲突看 `kind='spectre'`（8 条）**，
  需求 15.5 从 2026-08-19 起就是可验收的（`citation.conflict_block` 在幽魂那条路上真跑着）。
  `kind='mod'` 仍是 0 条，而**那是一个结论、不是缺口**：第三十一轮把配对判据从
  「组名」换成「阶梯指纹」后 2729 → 254，逐条看完剩下的 32 条全是不同词缀的巧合配对，
  也就是**可比范围内两源没有真正的数值分歧**。
  （这一段本身曾经写着「缺的是人工核对，不是代码」——那句判断是错的，
  真正的原因是配对判据坏了。**文档里的因果结论也会过期，用之前先抽验。**）
- **`source` 分四档，采集优先级只看 `chat`**：`eval` 是评测流量（实测占 96%）、
  `import` 是采集脚本、`verify` 是对账脚本。后两档记的是「缺多少条」而不是「有人问了几次」。
  **`verify` 那一档到第六十四轮才真的有行**：核对脚本的 UPSERT 在 `ON CONFLICT` 时
  不更新 `source`，于是它写的 11 条一直停在 `chat` 上、排在「真实用户缺口」头部，
  而它们在第三十四轮就已经被修掉了。修完之后真实用户缺口只剩 **2 条**。
  拿这张表当依据时，先确认它此刻在说什么。
- 问某个领域为什么答不上来时，先看它是 `harvested` 还是 `not_collected`：
  前者对用户要说「本库未采集该领域」并给权威来源，**不能说成「查不到」**——
  后者会让用户以为游戏里没这东西。
