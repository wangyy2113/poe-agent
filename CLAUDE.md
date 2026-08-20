# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

面向《流放之路 1》的领域 AI Agent：把 POEDB（P1）与 Path of Building `src/Data/`（P2）做成
可检索、可溯源的知识库，在此之上回答机制 / 词缀 / 装备 / 制作类问题。

单进程、单 SQLite 文件、零外部服务。规格真源是 `.kiro/specs/poe1-agent/`
（requirements / design / tasks），工程约定真源是 `.kiro/steering/project-conventions.md`。

## 大前提：只覆盖《流放之路 1》

**POE1，不是 POE2（流放之路：降临 / Path of Exile 2）。** 两款游戏大量概念同名
（升华、词缀、通货、地图、暴击），拿 POE1 的数值回答 POE2 的问题，用户看不出来——
而「一个错误的小前提会让整条结论反向」正是第一原则要防的。所以这条边界由代码执行，
分四处：

| 位置 | 做什么 |
| --- | --- |
| `orchestrator/loop.poe2_marker()` | 在**原始问题串**上匹配 POE2 标记，命中即由代码生成越界声明——不检索、不调模型 |
| `kb/fetcher.reject_poe2_host()` | 抓取入口硬护栏：`poe2db.tw` / `poe2.ninja` / `poe2wiki.net` 一律拒绝 |
| `agents/knowledge.py` 提示词 | 明确写「非 POE1 的问题（包括《流放之路 2》）直接说明不在服务范围」 |
| 黄金问答集 | 4 条 POE2 拒答用例 + 1 条反例（POE1 的「先祖降临」不得被误拦） |

两个必须记住的坑：

- **靠覆盖率拦不住 POE2 问题**，只有专指标记能拦。分词会把数字标记吃掉：
  `poe2 词缀怎么洗` → `['poe', '词缀']`、`流放之路2 的升华机制` → `['流放', '升华', '机制']`。
  实测「poe2 词缀怎么洗」因此拿到 100% 覆盖率、被判**依据充分**。所以判据只能在原串上匹配。
- **单独的「降临」不能当标记**：POE1 有天赋节点「先祖降临」「惊寒降临」。
  这与 boss 领域曾把「怪物」、路由曾把「知识库」当意图词是同一种错——意图词必须专指。

数据源也只认 POE1：`poedb.tw`（注意 `poe2db.tw` 才是 POE2，两个域名只差一个字符）、
PoB Community 的 PoE1 版（库里 `game_version` 全是 `3.29`；POE2 用 0.x 且是独立的 app）。
将来若要接 GGG `api/leagues` 动态解析赛季名，**那个端点会同时返回 POE2 赛季，必须过滤**。

## 第一原则：无据不答（改任何代码都受它约束）

禁止输出经验推断。查不到就输出缺据声明，不给「听起来合理」的答案。这条原则**落在代码里，
不靠提示词祈求**，两个由代码而非模型决定的闸门是整个设计的支点：

1. **入口**：`retrieval/hybrid.retrieve()` 判定依据是否充分，`sufficient=false` 时
   `orchestrator/loop.py` 直接走 `agents.knowledge.render_gap()`，**根本不调用 LLM**
   （所以缺据路径的 `tokens` 为空、完全可预测）。
2. **出口**：`orchestrator/binding.verify()` 逐句校验事实断言是否绑定有效证据编号
   `[[E1]]`，并把句中每个数值回填到它绑定的证据原文里比对；未通过的句子由代码替换为拦截说明。

由此派生的三条不可妥协的规则：

- **拒答粒度是单条断言**，不是整轮回答。收紧规则会带来「错误拒答」，它与「无据断言漏出」
  必须成对压制——`tests/unit/test_binding.py` 里「该拦的拦住」与「该放的放行」是成对写的，
  评测里两项也永远一起报。
- **「实体在库里」不等于「被问的维度有数据」。** `kb/coverage.py` 的 `dimension_gap()` 是
  充分性判定之后的第二道闸门：问题命中的领域若未采集、或 `partial_note` 明确排除了被问的
  维度，即使别名精确命中也走缺据声明。它**只能把「充分」改判为「不足」**——放宽依据要有
  新证据，而这里没有引入新证据。
- **偏好工具只豁免「BM25 命不中」，不豁免维度闸门。** 两者是不同的判断：前者说证据
  不在 chunk 里（那正是工具要去查的），后者说**这一维根本没有数据**——工具也造不出来。
  实测（第十八轮）：「大星团珠宝的斧剑伤害能开出哪些核心天赋」这个映射本库没有，
  而「星团」命中偏好工具后整轮被豁免、进了模型，结果模型写 12 句、出口拦掉 11 句，
  用户看到一篇拦截说明。**该拦的地方是入口，不是出口。**
  同一处还修了另一半：覆盖层先前只在「检索充分」时才被问，于是偏好工具那条路上它从未被咨询。
- **算出来的数字要连算式一起给，而且输入不许工具替用户先算。** `resistance_calc`
  的证据是一张逐步推导表（加总 → 截断 → 夹上下限 → 承受倍率），每个中间值都在里面，
  所以模型复述时数值回填校验能过。第二十轮实测到反面：地图减抗那条问题里模型
  自己先算了 `120 − 60`，只把 `60` 传进工具，于是证据里没有 `120`，
  它写的「120% − 60% = 60%」被自己的闸门拦掉——**一句完全正确的话，
  因为工具没给它表达输入的地方**。修法是给减抗单开一个参数（`resist_penalty`），
  不是放宽闸门。
- **在图上算出来的数字要连路径一起给。** `passive_path` 的点数是 BFS 结果，不是原文，
  所以它把整条路径逐节点列进证据——用户能照着核对。这是「算」与「猜」的分界：
  算的东西必须能被复核到边一级（`passive_graph_edge` 的 3,382 条）。
  同理三条排除（树心 root / 升华节点 / 大师天赋）都是**游戏规则**并写在表注释里：
  不排除 root，「野蛮人 → 女巫」会算成 2 点，实测真值 22。
- **续写结构是一个断言单位**：Markdown 表格（含表后**单独一行**的 `[[E1]]`）、
  以及**引导句后紧跟的列表项**。绑定标记出现在结构内任何位置都对整块生效，
  数值仍逐个回填校验（序号列除外）；流式侧没收齐**不下发**，
  且表格要**多等一行**——那一行可能只有标记（实测提前放行会让一致率掉到 64/66）。
  放宽只到「续写」为止：普通句子、链式继承（跟着上一个列表项）、`>` 引用块都不继承。
  逐条看完 42 句候选之后只有「引导句 + 清单」是真的续写，其余拦得对
  （判据见 `binding._is_list_lead_in` / `_list_lead_in`）。
- **流式粒度是句不是字。** 未校验内容一旦推给用户就撤不回来。增量校验由
  `binding.StreamingGate` 做，它**复用批量 `verify()` 的分句与判定函数**，绝不另写一套增量
  规则（`tests/unit/test_streaming_gate.py` 用 10 组文本 × 4 种切片粒度钉住两者结果一致）。
- **推理内容绝不外发。** `reasoning_content` 是模型动用参数记忆猜测的地方，正是闸门要拦的
  东西；`core/llm.py` 在类型层面就不让它流出，前端只显示「思考中」。

## 常用命令

```bash
# 环境初始化（跨平台唯一真源；.sh/.bat 只是薄封装）
python scripts/setup.py                    # --recreate 删 .venv 重建
.venv/bin/python scripts/selfcheck.py      # 配置/DB/FTS5/向量扩展/凭证 体检

# 启动（监听 127.0.0.1:8787，所有路由在 /poe_agent 前缀下）
.venv/bin/python main.py
# 端口被占用即退出（单实例保护靠绑定失败，不用 PID 文件）。
# 进程名被改成 poe-agent，pkill -f main.py 找不到它：lsof -nP -iTCP:8787 -sTCP:LISTEN -t

# 采集（POEDB）
.venv/bin/python -m kb.pipeline --category all
#   category: unique_item | unique_l2 | mods | bench | metamods | tree | monsters
#             | currency | scarab | maps | coverage | all
#   --harvest 只抓缓存不抽取 / --force 忽略缓存 / --rebuild 清空重建
#   --allow-bench-shrink、--allow-metamod-shrink、--allow-monster-shrink
#   只在人工核对上游确实删项后使用

# PoB 导入（需本地有 PathOfBuilding 检出；POB_PATH → config.yaml → ../PathOfBuilding）
.venv/bin/python scripts/probe_pob.py --align     # 先清点确认解析无误
.venv/bin/python scripts/import_pob_constants.py  # 游戏常量（calc/ 的真源）
.venv/bin/python scripts/import_pob_mods.py       # 词缀（--dry-run 只体检）
.venv/bin/python scripts/import_pob_uniques.py    # 唯一装备（自动过滤历史变体）
.venv/bin/python scripts/import_pob_bases.py      # 底材，18 个装备类别（--dry-run 先清点）
.venv/bin/python scripts/import_pob_skills.py     # 技能石与辅助宝石（中文名取自已缓存的 POEDB 清单页）
.venv/bin/python scripts/import_pob_spectres.py   # 幽魂抗性与建模系数，并与 POEDB 侧对账
.venv/bin/python scripts/verify_pob_mods.py       # 与 POEDB 对账
```

测试用标准库 `unittest`，**不引入 pytest**，也没有 lint 配置文件（`noqa` 码沿用 ruff 习惯）：

```bash
.venv/bin/python -m unittest discover -s tests -t .          # 全量，当前 813 项，秒级
.venv/bin/python -m unittest tests.unit.test_binding         # 单个模块
.venv/bin/python -m unittest tests.unit.test_binding.TestVerify.test_xxx   # 单个用例
.venv/bin/python -m unittest discover -s tests -t . -k tier  # 按名筛选
```

**改闸门或检索阈值之前先跑评测拿基线，改完再跑一次对比**——这是本项目唯一能判断
「收紧规则」到底是变好还是变坏的手段（错误拒答与无据漏出必须成对看）：

```bash
.venv/bin/python scripts/eval_run.py                 # 检索侧，全离线
.venv/bin/python scripts/eval_run.py --sweep 0.3,0.5,0.7   # 扫 min_coverage 的成对代价
.venv/bin/python scripts/eval_run.py --gate          # 出口闸门，用已有录制离线重放
.venv/bin/python scripts/eval_run.py --audit         # 刷新人工审计清单并算三项人判指标
.venv/bin/python scripts/eval_run.py --record        # 重录真实响应（**要凭证、烧 token**）
```

黄金问答集是 `tests/golden/questions.yaml`（96 条，六类），基线与全部结论在
`tests/golden/BASELINE.md`——**动闸门前先读它**，里面记了多个已修缺陷与三条已知未修边界，
以及两次「测量本身写错」的教训。

单测全部离线（解析器不连库，T 级匹配用内存 SQLite 打桩，LLM 与外部源打桩）。
需要跑真实链路时用脚本：`scripts/verify_chat.py`（`/chat`、`/chat/stream`、`/item` 端到端，
`--gap-only` 只测不需要凭证的缺据路径）、`scripts/test_search.py`、`scripts/probe_gateway.py`
（网关能力探测）、`scripts/verify_llm.py --offline`。

## 请求生命周期

```
POST /chat[/stream]  api/chat.py（只做校验与编排调用）
  └─ orchestrator/loop.py  run_turn / stream_turn
       ├─ 强制检索        retrieval/hybrid.py：别名精确 + FTS5/BM25 + 词缀意图三路
       │                   → RRF 融合 → 来源优先级衰减 → 充分依据判定
       ├─ 不充分          agents.knowledge.render_gap()（代码生成，不经模型）→ 结束
       ├─ 充分            binding.number_evidence() 给证据编号 E1..En
       ├─ LLM 循环        core/llm.py（多 Key 故障转移）+ tools/registry.py 工具调用
       ├─ 出口校验        binding.verify() / StreamingGate（逐句拦截）
       ├─ 答案附注        _answer_notes()：截断计数 / 覆盖声明 / 权重口径，代码生成不过闸门
       └─ 引用装配        orchestrator/citation.py，落轨迹进 trace 表
```

其他入口：`POST /item`（装备文本 → 结构化报告，**全程不调 LLM**，因此不过出口闸门——闸门防
的是模型编造）、`POST /search`（调试用，先看 `sufficient` 字段）、`GET /healthz`、
`GET /admin/*`（`Authorization: Bearer $ADMIN_TOKEN`，未配置也返回 403 而非放行）。

## 模块职责

| 目录 | 职责与要点 |
| --- | --- |
| `core/` | `config.py` 双层配置（`.env` 密钥 + `config.yaml` 声明式行为，路径都相对 `REPO_ROOT`）、`db.py` 单文件 SQLite + 向量三级探测、`llm.py` 凭证池与推理模型适配 |
| `api/` | 路由层，**只做校验与编排调用，不写业务逻辑**；`deps.py` 鉴权 |
| `orchestrator/` | `loop.py` 主循环与轨迹（批量与流式**共用** `_prepare_turn` / `_handle_tool_call` / `_terminal_gap`，只在「怎么把文本交给用户」上不同——不要在任一侧重新写判据，`tests/unit/test_turn_parity.py` 会挡）、`binding.py` 出口闸门（1400+ 行，改动前先读它的模块 docstring）、`citation.py` 引用装配 |
| `agents/` | `knowledge.py`：系统提示词 + `ALLOWED_TOOLS` + 缺据声明模板。提示词是第一道约束不是唯一约束 |
| `tools/` | `registry.py` 注册与 schema 导出，`impl/` 各工具；返回值统一 `ToolResult`，`citations` 是结构化引用而非拼进正文的字符串（要参与出口校验） |
| `retrieval/` | FTS5、别名、RRF 融合、jieba 分词、领域词表 |
| `kb/` | `pipeline.py` 采集管线、`sources/` 各来源抽取器、`coverage.py` 知识库自描述层、`stat_channels.py` 属性来源渠道注册表、`render*.py` 卡片切片 |
| `parsers/` | `item_text.py`（纯文本，不连库）→ `item_mods.py` T 级匹配 → `mod_bridge.py` 英文桥接 → `item_report.py` 报告 |
| `craft/` | `feasibility.py` 词缀组合三态可行性、`pool.py` 胸甲普通词缀有效池与条件概率 |
| `calc/` | `constants.py` 常量访问层，**缺失即抛异常，绝不给默认值**；`derive.py` 两个计算器共用的推导件（`Step` / 引用推导）；`resistance.py` 抗性结算；`mitigation.py` 受击结算（闪避/命中、格挡、法术压制、护甲）；`recovery.py` 回复（护盾回充、偷取、魔力再生）。暴击 / 保留 / Max Hit / 持续伤害未实现 |
| `static/` | 单页前端三个文件，无构建步骤、无 Node、无第三方 JS（Markdown 渲染器自写，为了强制先转义再解析） |
| `tests/golden/` | 评测资产：`questions.yaml` 黄金问答集、`loader.py` 严格校验、`recording.py` 录制回放、`recordings/` 真实响应、`audit.yaml` 人工结论、`BASELINE.md` 指标基线 |

## 工具集：能精确算的不要猜

`agents/knowledge.ALLOWED_TOOLS` 的**顺序即推荐优先级**，注册在 `tools/impl/`（导入
`tools.impl` 即完成注册）：

`affix_pool_odds`（胸甲同侧普通词缀条件概率）→ `check_mod_combination`（三态可行性）→
`bench_recipe_catalog`（工艺台目录）→ `metamod_interaction_matrix`（逐字矩阵单元格）→
`mod_tiers`（T 级梯度）→ `skill_gem_levels`（宝石逐级字面值）→
`spectre_stats`（幽魂倍率 / 抗性 / 排名）→ `passive_path`（点到某天赋要几点 + 逐节点路径）→
`cluster_jewel_info`（星团珠宝尺寸 / 基石 / 是否在主树外）→
`resistance_calc`（按 PoB 原式复算抗性，给逐步推导）→
`mitigation_calc`（受击前几段：闪避/命中、格挡上限、法术压制、护甲减伤）→
`recovery_calc`（护盾回充 / 偷取两层上限 / 魔力再生）→
`stat_sources`（某属性能从哪来）→
`entity_lookup` → `kb_coverage` → `kb_search`（兜底）

新增工具时守住两条：**能力边界由 `ALLOWED_TOOLS` 限定而不是靠提示词描述**；工具描述里不要
手写「尚未采集 X」这类清单——那种句子在数据入库当天就变成假话，未采集声明应由
`kb/coverage.py` 的 (inventory × status) 差集派生。枚举类工具静默截断等同于漏答，超出上限
必须写出还剩多少。

## 数据层

全部状态在 `data/poe.db` 一个文件（WAL），复制该文件即完成备份。表分工见 `core/db.py` 的
`SCHEMA` 注释，三条容易踩的建模决定：

- `mod_affix` 按「词缀 × 底材类别」一行（同一词缀在弓上 T5、法杖上 T4 是两行），主键是实质
  字段全集的哈希，**不跨类别去重**；互斥判断用整个 `family_list` 求交集，T 级分组只用
  `correct_group`。
- `pob_mod` 单独建表而不是塞进 `mod_affix`：两源粒度不同，且交叉校验要求各自独立保存，
  混表会让 UPSERT 在冲突被发现前就互相覆盖。对账结果落 `source_conflict`，其中
  `kind=mod` 是已确认冲突（并列展示给用户），`mod_review` 只是待人工核对，**不得当作冲突呈现**。
- `pob_skill_level` **只放字面值，没有伤害列也没有属性需求列**。PoB 存的每级伤害是乘数
  （冰霜新星 1 级存 `0.8 / 1.2`），`reqStr/reqDex/reqInt` 是换算系数（真需求要按
  `round((20+(等级需求-3)×3)×(系数/100)^0.9×statType)` 算）。两者都得按 PoB 的公式算才是
  游戏里的显示值，算错就是「带着引用的错数字」——比查不到更糟。也**不留空列**：留了下一个人
  会以为它待填，然后把乘数当伤害写进去。这两维明确划进 `partial_note` 的未采集部分。

本机 `data/poe.db` 现状（`GET /healthz` 与 `/admin/stats` 可查）：`entity` 5197、
`mod_affix` 93897、`pob_mod` 25487、`chunk` 18447、`alias` 8106、`game_constant` 508、
`craft_bench_recipe` 783、`pob_base_item` **1022**（18 个装备类别，PoB × POEDB 逐条桥接，
覆盖 PoB 侧 96.1%；未桥接的 41 条已按类别登记在台账）、`source_conflict` **515**（`mod_review` 254 + `unique_review` 207 + `spectre_review` 43
+ `spectre` **8** + `tree_review` 3）。

`pob_skill_gem` **810** 颗宝石（548 技能石 + 262 辅助）+ `pob_skill_level` **30422** 行
每级数值。桥接键是 PoB 的 `grantedEffectId`（它也出现在 POEDB 清单页的 `data-hover` 里），
不分大小写配对命中 810/861、零多解；未桥接的 62 条按成因登记在台账
（`pob_missing` 51 = POEDB 保留的历史/未上线条目，`no_zh_name` 11 = 取不到中文名）。
**这两张表刻意没有伤害列，也没有属性需求列**——理由见 `core/db.py` 的表注释，
那是本轮最重要的一条边界。

`poedb_currency` **113** 条通货（中英文名、堆叠上限、效果原文；其中 **15** 条在 POEDB 上
没有效果文本——碎片与命能类，留空而不是自己补一句）。那一页 `<table>` 计数为 **0**，
先前「已抓取未入库」读起来像是抽不出内容——**实测它是卡片布局**，内容在
`a.item_currency` / `div.property` / `div.explicitMod` 三个 class 里。
**结构判断要去看 DOM，不要看表格计数**（这是同一类错误的第六次）。
工艺台费用引用的 18 个通货按中文名与 slug **都 100% 命中**这张表。

`poedb_scarab` **118** 个圣甲虫（效果、堆叠上限、**地图装置放置上限**——页面上的「限制」，
与堆叠上限是两回事，混一起会把「最多放几个」答成 20）。那一页有三个物品类，
只收 `MapFragment`；4 条亡者之城包与 1 条孵化器按物品类登记进台账。
卡片解析与通货共用 `kb/sources/poedb_cards.py`（两页结构逐字相同），分开的只是表与域。

`poedb_map` **159** 张地图（等阶——只有 110 张有，梦魇/传奇等特殊地图页面未给；头目、
额外目标、掉落的命运卡、打完头目解锁的工艺台配方，以及**异界图鉴上相邻的地图**，159 张全有）。
这个域挂了三页而**只有 `Atlas_of_Worlds` 有实质内容**：`Maps` 页主表是「地图碎片 → 区域」
且区域名是未翻译的英文 id，`Atlas` 页只有 1 张中文卡片。**同一个域下的页面要一页一页看。**

`pob_cluster_notable` **308** 条星团珠宝核心天赋与基石（中文名由已入库的天赋卡按英文名桥接，
100% 命中）+ `pob_cluster_jewel` 3 个尺寸（小 2–3 / 中 4–6 / 大 8–12 个小天赋）
+ `pob_cluster_skill` 55 条小天赋类型（**仅 PoB 英文原文**——中文附魔文本不在任何已缓存页上）。
**这一轮补的是分类不是数据**：那 300 个核心天赋的属性早就随天赋树入库成了天赋卡，
缺的是「它们只能从星团珠宝拿、不在主树上」这个事实——`passive_path` 先前只能说
「可能只存在于星团珠宝或升华子树」。**哪些核心天赋出现在哪种小天赋类型上仍未采集**
（本地来源没有这个映射）。

`passive_graph_node` **3390** 个天赋树节点（其中可经过 **2478**）+ `passive_graph_edge`
**3382** 条无向连线 + `passive_tree_meta`（`totalPoints` 123、`ascendancyPoints` 8）。
节点属性早就入库了，缺的一直是「怎么连的」——树 JSON 本来就在缓存里也一直被解析，
只是 `in`/`out` 与 `classes` 没被取出来。节点 id 就是 GGG 的 skill id
（实测 3389/3389 与 JSON 键相等），所以能与天赋卡的 `skill_ids` 对上。
**只有主天赋树入图**：Atlas 树没有职业起点，点数规则也是另一套。

`pob_spectre` **268** 条 PoB 幽魂建模数据（四抗性、护甲/闪避/能量护盾系数、攻击间隔、
怪物技能 id）。它同时是一份「玩家实际会用」的确认清单，倍率排名只在这 268 个里做——
POEDB 那 1948 条里混着开发用怪物（「测试之灵」10000%/10000%）。

`poedb_monster_mod` **183** 条怪物词缀（传奇 / 魔符 / 惊悸迷雾 / 猎魔 / 罪恶枷锁，
有中文文本的那些）+ `poedb_spectre` **1948** 个可作幽魂的怪物与倍率。同一张
POEDB `/cn/Monsters` 页还有按等级的基础数值表，**刻意不取**——PoB 侧的
`monsterLifeTable` 等早已在 `game_constant` 里，重复入库等于制造两份可能不一致的真源。
幽魂那张表**没有卡片**，只有工具 + 意图词路由（理由见 `kb/render_monsters.py`）。

`craft_metamod_interaction` **134** 行（5 个小节 × 34 行操作，2 个空白格只进审计不成为
证据）。这张表长期是 0 行，而文档一直写着「需联网跑」——**那句是错的**：pipeline 只抓
POEDB 镜像，而镜像页一直在 `data/http_cache` 里、抽取器对它跑得通，这条采集只是从来
没被跑过。教训：「需要联网」这种判断要去看代码抓哪个 URL、缓存里有没有，别照抄注释。

`game_constant` 新增 `pobMisc` 组 **2** 条（`ResistFloor` −200、`MaxResistCap` 90），
它们来自 PoB 自己的 magic numbers 表 `Modules/Data.lua` 而**不是** `Data/Misc.lua`，
因此 `dat_source` 为空——那张表开头就写着 `-- magic numbers`，里面既有游戏真值也有 PoB
建模取值，还混着表达式与**重复键**（`EnergyShieldRechargeBase` 出现两次）。所以按
**白名单**取（`kb/sources/pob_misc.MODULE_MISC_WHITELIST`），只取计算器真正需要的；
整表入库会把「有没有 GGG 原始表出处」这条区别抹掉，而这个库的价值正在于每个数字都说得清出处。

抗性计算器（`calc/resistance.py` + `resistance_calc`）复算的是 PoB 的
`calcs.resistances`：**加总 → 乘「增加的抗性」→ 截断小数 → 夹在 −200% 与最大抗性之间**，
最大抗性本身是 `min(90%, 75% + 各项加成)`。三条容易搞反的结论都由代码顺序决定，
不是经验之谈：

- **超抗顶得住减抗与诅咒，顶不住穿透**。减抗进的是加总那一步（夹上限**之前**），
  而穿透在 `resMult = 1 - (resist - enemyPen) / 100` 里减（夹上限**之后**）。
- **混沌抗性不吃「+X% 全部元素抗性」与「+X% 最大元素抗性」**（`isElemental` 只有火冰电）。
- **剧情抗性惩罚 −60% 是 PoB 建角色时就加的 BASE**（`CalcSetup.lua:503`，三档 0/−30/−60）。
  工具**默认不加**：角色面板上的抗性已经含它，替用户再减一次就是把一个对的数字算错。

这一轮引入的验证方式对后面每一块计算器都适用：**公式要钉住上游原文**。
`tests/unit/test_resistance.py` 一边拿 **PoB 自己的测试期望值**对账
（`spec/System/TestDefence_spec.lua:386` 断言 `LightningResistTotal == 90`、
超抗 15，差额正是那个 −60），一边直接读本地检出断言我建模的那几行**原文仍在、
且引用的行号锚点确实指向它们**。数据漂移有 `check_doc_facts.py` 拦，
**公式漂移此前没有任何东西拦**——第一次跑就抓到 `TAKEN_LINE` 差了一行。

`pobMisc` 组现有 **6** 条（第二十一轮加了 `EvadeChanceCap` 95、`BlockChanceCap` 90、
`SuppressionChanceCap` 100、`SuppressionEffect` 40）。**`DamageReductionCap` 故意不收**：
上游那一项写成 `data.characterConstants["maximum_physical_damage_reduction_%"]`——
是个引用而不是字面量，而被引用的那条早就在库里、还带着 GGG 原始表出处。
收它只会多一份出处更差的副本。抽取器对这种项会报「不是数值」，那正是白名单该有的反应。

受击结算（`calc/mitigation.py` + `mitigation_calc`）复算 PoB 的四段，**顺序本身就是答案的一部分**：
**能不能命中（闪避）→ 能不能挡下（格挡 / 法术压制）→ 打中了减多少（护甲与物理减伤）→ 抗性**。
四条容易记错的：

- **护甲不是固定百分比减伤**：`护甲 /(护甲 + 5 × 这一击的伤害)`。所以
  「10000 护甲能减多少伤害」**在不给伤害数值时没有答案**，工具说缺什么而不是替用户
  假定一个伤害值（黄金集里 `num_armour_reduction` 与 `gap_armour_without_damage` 成对）。
- **命中率下限 5%、闪避率上限 95%**：闪避堆到无穷也躲不掉全部攻击。
- **法术压制减 40%，不是 50%**（`pobMisc.SuppressionEffect`）。
- **格挡几率超出最大格挡的部分不生效**，要先抬高最大格挡（基础 75%、硬上限 90%）。
  这条与 PoB 自己的用例对账（`TestDefence_spec.lua:573`：100% 法术格挡 → 生效 75、超出 25）。

怪物命中值不用用户提供：`monsterAccuracyTable` 就在库里（100 级），而 PoB 给敌人加命中
就是 `monsterAccuracyTable[enemyLevel]`。**但用了哪一级要写进推导**——「85 级」这个前提
是查出来的，不是假定的。等级越界抛异常不外推。

`pobMisc` 组现有 **9** 条（第二十三轮加了 `EnergyShieldRechargeBase` 0.33、
`EnergyShieldRechargeDelay` 2、`LeechRateBase` 0.02）。回复计算器
（`calc/recovery.py` + `recovery_calc`）三段各有一个容易记错的点：

- **护盾回充要先等 2 秒**，且每秒回的是**最大护盾的 33%**（所以回满约 3 秒、
  与护盾大小无关）。「多久回满」= 延迟 + 池 ÷ 每秒量，只说一段就答错。
- **偷取有两层上限**：速率上限（每秒最多回复池的 20%，护盾只有 10%）与
  **单次实例上限**（一次偷取最多回复池的 10%）。前者限速、后者限量。
  三种池的值不同，所以按池分别取常量、不合并。
- **魔力再生基数是每分钟 105%**（每秒 1.75%）。拿 105 当每秒用会差 60 倍。

**白名单里唯一一个重复键**：`EnergyShieldRechargeBase` 在 `Modules/Data.lua` 里写了两次，
先是 `characterConstants[...] / 60 / 100`（表达式），紧接着一行字面量 `0.33`。
Lua 后者覆盖前者，解析器同样取后者——**行号锚点也因此改成取最后一次赋值**
（先前取第一次，点开看到的是一行没有被采用的代码）。两种写法差**正好 1%**
（0.33 vs 0.3333…），所以 PoB 显示的回充速度比 GGG 常量算出来的低 1%；
我们复算的是 PoB，就照它取 0.33，但**把这条差异写进证据**，
不让用户以为 33% 是游戏的精确值。这类「同一个数两处不一致」要证明而不是相信
（判据在 `tests/unit/test_recovery.py`）。

一处数据缺口会直接影响判断：

- **需求 15.5「跨来源冲突并列展示」在 2026-08-19 第一次真正接上。** 在那之前
  `citation.conflict_block` 是**不可达代码**：`kind='mod'` 的已确认冲突长期是 0 条。
  幽魂对账给出 **8 条真分歧**（`kind='spectre'`，两侧各只有一条却对不上）之后，
  `spectre_stats` 把渲染好的冲突块放进 `data['conflict_block']`，由
  `loop._NOTE_FIELDS` 逐字附在答案末尾——**不让模型转述**：那几个数字来自两个来源，
  模型说出来只能无绑定地说，然后被自己的闸门拦掉。
  `kind='spectre_review'`（43 条，POEDB 记得更细的粒度差异）**不得当作冲突呈现**。

- **`source_conflict` 里 `kind='mod'`（已确认冲突）是 0 条，而这次是个结论、不是缺口。**
  `mod_review` **254** 条（`ladder_ambiguous` 222 + `ladder_partial` 32，见 `cause` 列）。

  原先 2729 条待核对、文档把卡点写成「缺的是人工核对，不是代码」——**那个判断是错的**。
  真正的原因是配对判据错了：对账按 `correct_group` 组名配对，而 `parsers/mod_bridge.py`
  早就实测过两侧组名系统性对不上（掉落词缀同名组仅 11.5%）。改用**阶梯指纹**配对后
  2729 → 254（−91%），区间一致率 86.5% → **92.7%**。

  逐条看完剩下的 32 条 `ladder_partial`：**全是不同词缀的巧合配对**（POEDB 武器局部命中值
  vs PoB「Minions have +(456-545) to Accuracy」，下界恰好都是 456；定值再生 vs 百分比
  充能速率；穿刺效果 vs 狂怒球）。也就是说**在可比范围内两源没有真正的数值分歧**。
  所以「跨来源冲突并列展示」（需求 15.5）没有东西可展示，`citation.conflict_block`
  仍不可达——但现在是因为**没有冲突**，而不是因为判据坏了。改数据源版本后要重跑对账。

## 技术栈边界（不要替换、不要引入）

- Python 3.12（最低 3.10），`pip` + `venv`，`requirements.txt` + `requirements.lock`。
  **不用 uv / poetry / conda，不建 `pyproject.toml`**，新增依赖须钉版本、同步 lock，
  并确认 Windows 有 wheel 或为纯 Python。
- LLM 走 `openai` SDK 直连内部 LiteLLM 网关的 `/v1/chat/completions`，主模型 `glm-5.2`，
  守门用 `claude-haiku-4-5`（`config.yaml` 的 `llm.gate_model`）。**不引入 `anthropic` SDK**，
  **不走 `claude` CLI 子进程**（拿不到增量流会破坏按句校验，还会把 Node 依赖引回来）。
- **不要引入** Docker、Qdrant 等需独立启动的向量库、Postgres、Redis、本地推理服务、
  Node 前端工程。向量通道是 `sqlite-vec` 同库扩展，且当前 `enabled: false`（网关未注册
  embedding 模型）——检索走 BM25 + 实体精确 + 词缀意图，`degraded=true` 是这个意思，不是故障。
- 跨平台硬规则（目标：迁 Windows 不改一行应用代码）：路径一律 `pathlib`；`open()` 必须显式
  `encoding="utf-8"`；不用 `fcntl`/`pwd`/`os.fork` 等 Unix 专有模块；不 shell 调
  `ps`/`grep`/`kill`/`nohup`；注册 `SIGHUP` 前 `hasattr` 判存在；日志按日期后缀命名不做
  rename 轮转；缓存文件名用哈希不用 URL 原文。平台差异只允许出现在 `scripts/`。

## 改代码时的既有坑（都是实测结论，别凭记忆改回去）

- `max_tokens` 有硬下限 `MIN_MAX_TOKENS=2048`（`core/llm.py`）。GLM 5.2 的推理 token 可占输出
  98%，设小了会**正文为空且不报错**——最隐蔽的失败模式。Token 计量必须含 `reasoning_tokens`。
- 默认不发 `temperature`：网关背后挂着 Bedrock，它直接拒该参数导致整次调用 400。客户端遇到
  「该模型不支持此参数」会丢参数重试。
- 响应里的 `model` 字段要留着：多 Key 故障转移会切模型，不记下来排查时会误判推理 token 行为。
- 充分依据判据是 `min_coverage`（查询实义词的命中比例）而**不是** `min_score`——相对归一化下
  最优项恒为 1.0，用分数做阈值等于闸门永不触发。
- **排序层反复加权、限流、调系数，往往说明索引切分粒度不对。先怀疑粒度，再改排序**
  （`retrieval/hybrid.py` 顶部记录了两个因此被删掉的补偿层）。
- 路由前缀用显式 `APIRouter(prefix=...)`，不用 FastAPI 的 `root_path`（后者假定反向代理会剥
  掉前缀，本机直连时路径对不上）。静态目录必须挂在 API 路由**之后**。
- 出口闸门判不出句子类型时**默认按事实句处理**（安全侧）。Markdown 标题、加粗标签、分隔线、
  缺据声明句已豁免；带数字的标题仍走事实句校验。
- **不在任何证据里的边界声明由代码附注，不靠模型说、也不靠白名单豁免**
  （`orchestrator/loop._answer_notes()`）：枚举截断的计数只在工具 `data` 里、覆盖声明是工具
  算好的、权重口径是提示词硬性要求的——模型说这些只能无绑定地说，然后被拦，用户就丢了信息。
  豁免则等于允许模型自己编这些数字。
- **改提示词是全局改动，必须先量再改。** 实测：加一段 9 行的说明（告诉模型某些声明不用自己
  复述）让引用有效率 67%→55%、拦截 91→122，只换来元说明类少 3 句，已撤销
  （`tests/golden/BASELINE.md` 第四轮有三向对比）。它不只影响被提到的那几句，会拖垮整体
  绑定纪律。另外**两次数字的差不能当因果**——模型有轮次波动，判因要有同条件重复运行。
- **「裁剪 schema 让模型只有一个工具可调」拦不住它一个都不调。** 实测（第二十一轮）：
  受击计算器刚接上时 5 条新录制里 3 条没调工具——一条直接拿检索到的 chunk 答了
  （路由观测 0/1），一条零证据进了兜底缺据，还有一条**碰巧**拒答对了、
  但理由是「工具没返回证据」而不是工具想说的「你没给这一击的伤害」：
  **契约过了，机制是坏的**。修法是把计算器放进 `_CODE_INVOKED_TOOLS` 由代码调用：
  没有数字就是讲规则（无参数），有数字则只认「数字 + 单位词紧邻」的写法、
  同一槽位抽到两个值就整体交回模型。这与 `mod_tiers` 是同一条纪律。
- **绑定标记的写法要放宽到模型真实的写法。** `[E1]`（单层方括号）也算绑定：
  92 条录制离线重放，143 句拦截里有 7 句只因为模型少写了一层括号，
  而被拦的是有据的话。放宽的是**写法**不是**判据**——数值回填照旧跑。
  这条改了一个此前刻意的决定（`test_malformed_markers_still_rejected` 曾把 `[E1]`
  列进残缺写法），依据是新测出来的数字：**放宽依据要有新证据，这次有。**
- **偏好工具的判据不能是「属性名」，只能是「问法」。** 实测（第二十一轮）：
  `mitigation_calc` 的意图词里放了裸的「法术压制」，于是「法术压制能从哪些渠道获得」
  被从 `stat_sources` 抢走——那是一道枚举题。修法不是逐个绕开裸属性名（想漏一个就
  又被抢一次），而是让**枚举问句永远不是计算题**：`_YIELDS_TO_ENUMERATION` 里的工具
  遇到 `_ENUMERATION_INTENTS` 一律让路。判据描述的是**问句形式**时比描述**话题**时更可信。
- **数值回填校验前必须把排版正负号归一成 ASCII**（`binding.normalize_signs`）。
  模型写「−200%」（U+2212）而证据原文写 `-200%`，提取器读出的是 `200`、在证据里找不到，
  于是把一句完全正确的话判成编造。**只映射真正的正负号**：连接号 `–`/`—`/`~` 在这个领域
  是区间分隔符（`(40–55)`），当成负号会把区间读成两个负数。抗性、减抗、诅咒全是负数场景，
  不归一等于对整类负数误拦。
- **拦截统计要按「形态」分类之后再下结论。** 第二十一轮把「表格被拦、附近有标记」
  笼统数成 6 句同类，第二十二轮逐条看才发现只有 1 句是「表后只有标记的一行」，
  另外 5 句是**表后跟正文**——两种形态的修法完全不同（前者并进表，后者不能并，
  否则表能从一句无关的话里借编号）。**先分形态再数数**，否则会照着一个混合口径去修。
- 装备分析不给市场价格：经济源未接入，三项价值判断一律输出「数据不足」+ 缺哪项数据。
  T 级查不到就标「未匹配」并写原因，不给推测值。
- 引用 PoB 必须用钉住 commit 的 GitHub blob URL；缺 `url` 或 `source` 的文档不得进可引用索引。

## 研发规约（迭代本项目的硬性工作方式）

完整版在 **`.claude/ENGINEERING.md`**（只有本文件会自动进上下文，那个不会——动手前读它）。
七条摘要：

1. **先全局 review 再动手**：读目标模块的 docstring、`BASELINE.md`、同一规则的其它实现处、
   相关 spec 条目。「全局」的意思是**先判断改动该落在哪一层**。
2. **顶层视野**：层次有优先序——代码生成 > 代码闸门 > 结构化工具 > 提示词，
   越靠上越可预测。**能在上层解决的绝不放到下层。**
3. **大胆重构、拒绝补丁**：同一概念多处各写一份判据、白名单加语义重叠的新项、
   按用例名特判的 `if`、排序层反复加权 —— 见到就停下来重构，不要再加分支。
   安全网是 450 项秒级单测 + `--gate` 离线重放 + 流式批量一致性，重构前后必须持平或变好。
4. **该删就删**：不留「以后可能有用」的函数，不为自己刚写的旧行为留兼容分支；
   约束本身是错的就删约束（`UNIQUE(name_zh)`），别绕开它。删除要证明没丢能力。
5. **防漏改**：成对点清单在规约第 5 条。最高频的是批量 vs 流式、提示词 vs 闸门。
6. **防埋坑**：先量后改，且**两次数字的差不能当因果**（模型有轮次波动，判因要重复运行）；
   先确认测量本身是对的。**注释与文档里的「因果结论」也要抽验**——`check_doc_facts.py`
   只拦数字漂移，拦不住判断。连续四轮里最值钱的产出都是发现项目自己写的判断是错的
   （「需联网」其实缓存里就有、「缺人工核对」其实配对判据错、「已排除整列」其实函数从未生效、
   「PoB 侧有值可回填」方向正相反）。规约 6b 有清单与抽验动作。
7. **改完就提 PR**：单测 + `--gate` + `check_doc_facts.py` 三步收口，
   `BASELINE.md` 记一轮（**包括被否掉的改动**），PR 写清指标前后对比。

## `.claude/` 里已经准备好的东西

- **`.claude/settings.json`**（入库、团队共享）：离线只读命令已在允许列表里（单测、
  `eval_run.py` 的各种只读模式、各 `verify_*`、`--dry-run` 导入、本机 curl、git 只读）。
  **会花钱或改数据的仍然会问**：`eval_run.py --record`、`verify_llm.py`、`verify_chat.py`、
  `probe_gateway.py`、`kb.pipeline`（除 `--category coverage`）、以及编辑 `.env`。
  `rm -rf data*` 一类直接拒绝。个人覆盖放 `.claude/settings.local.json`（已 gitignore）。
- **一条 PostToolUse hook**：改到 `orchestrator/binding.py`、`orchestrator/loop.py`、
  `retrieval/hybrid.py`、`kb/coverage.py` 时提醒跑成对评测。它守的是本项目最容易忘的规矩——
  收紧规则会带来错误拒答，放宽会带来漏出，只压一头不算完成。
- **`.claude/ENGINEERING.md`**：研发规约完整版（上一节是摘要）。
- **四个 slash command**：`/check`（单测 + 自检 + spec 引用）、`/eval`（跑评测并与基线对比，
  含「什么时候需要重录」的判据）、`/gap`（缺口台账与覆盖现状，含判读陷阱）、
  `/review`（全局 review：补丁症状、成对点漏改、死代码扫描）。
  它们的价值主要在**判读要点**那几段，不只是命令本身。

## 文档与仓库约定

- 面向用户的输出用简体中文，关键术语保留英文原文（如「法术压制（Spell Suppression）」）。
  代码标识符用英文；注释与 docstring 沿用既有风格——**中文，且优先解释「为什么这么写」并写下
  实测数据**（steering 里那句「注释用英文」已被全仓库实践覆盖，按代码走）。文档与提交信息用中文。
- 提交信息形如「实现胸甲普通词缀条件概率闭环」——一句话说清这次闭环了什么能力。
- `data/`、`logs/`、`.env`、`.venv/` 已 gitignore。`semantic-review/` 是本地差异审查产物
  （未入版本库），不是运行时依赖。
- `.kiro/specs/poe1-agent/tasks.md` 的复选框**只在阶段 1b 之后可信**：阶段 0/1/2 的条目大量
  未勾选但代码早已落地，判断现状请看代码与 `/healthz`，不要以复选框为准。
  `scripts/check_spec_refs.py` 可校验 spec 内的需求交叉引用。
- `README.md` 与 `tasks.md` 已在 2026-08-17 同步过一轮（单测数、`craft/`、新采集类别与工具、
  评测体系、V1 验收清单的实测结论）。再改这些区域时顺手更新，别让文档重新落后于代码——
  上一次落后的代价是「140 项单测」这类数字误导了对现状的判断。
