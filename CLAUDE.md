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
- **Markdown 表格是一个断言单位**，不是若干行。绑定标记出现在表内任何位置都对整张表生效，
  数值仍逐个回填校验（序号列除外）；流式侧表格没收齐**不下发**。
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
#   category: unique_item | unique_l2 | mods | bench | metamods | tree | coverage | all
#   --harvest 只抓缓存不抽取 / --force 忽略缓存 / --rebuild 清空重建
#   --allow-bench-shrink、--allow-metamod-shrink 只在人工核对上游确实删项后使用

# PoB 导入（需本地有 PathOfBuilding 检出；POB_PATH → config.yaml → ../PathOfBuilding）
.venv/bin/python scripts/probe_pob.py --align     # 先清点确认解析无误
.venv/bin/python scripts/import_pob_constants.py  # 游戏常量（calc/ 的真源）
.venv/bin/python scripts/import_pob_mods.py       # 词缀（--dry-run 只体检）
.venv/bin/python scripts/import_pob_uniques.py    # 唯一装备（自动过滤历史变体）
.venv/bin/python scripts/import_pob_bases.py      # 底材，18 个装备类别（--dry-run 先清点）
.venv/bin/python scripts/verify_pob_mods.py       # 与 POEDB 对账
```

测试用标准库 `unittest`，**不引入 pytest**，也没有 lint 配置文件（`noqa` 码沿用 ruff 习惯）：

```bash
.venv/bin/python -m unittest discover -s tests -t .          # 全量，当前 411 项，秒级
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

黄金问答集是 `tests/golden/questions.yaml`（53 条，六类），基线与全部结论在
`tests/golden/BASELINE.md`——**动闸门前先读它**，里面记了四个已修缺陷、两个已知未修边界，
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
| `calc/` | `constants.py` 常量访问层，**缺失即抛异常，绝不给默认值**；计算器本体未实现 |
| `static/` | 单页前端三个文件，无构建步骤、无 Node、无第三方 JS（Markdown 渲染器自写，为了强制先转义再解析） |
| `tests/golden/` | 评测资产：`questions.yaml` 黄金问答集、`loader.py` 严格校验、`recording.py` 录制回放、`recordings/` 真实响应、`audit.yaml` 人工结论、`BASELINE.md` 指标基线 |

## 工具集：能精确算的不要猜

`agents/knowledge.ALLOWED_TOOLS` 的**顺序即推荐优先级**，注册在 `tools/impl/`（导入
`tools.impl` 即完成注册）：

`affix_pool_odds`（胸甲同侧普通词缀条件概率）→ `check_mod_combination`（三态可行性）→
`bench_recipe_catalog`（工艺台目录）→ `metamod_interaction_matrix`（逐字矩阵单元格）→
`mod_tiers`（T 级梯度）→ `stat_sources`（某属性能从哪来）→ `entity_lookup` →
`kb_coverage` → `kb_search`（兜底全文检索）

新增工具时守住两条：**能力边界由 `ALLOWED_TOOLS` 限定而不是靠提示词描述**；工具描述里不要
手写「尚未采集 X」这类清单——那种句子在数据入库当天就变成假话，未采集声明应由
`kb/coverage.py` 的 (inventory × status) 差集派生。枚举类工具静默截断等同于漏答，超出上限
必须写出还剩多少。

## 数据层

全部状态在 `data/poe.db` 一个文件（WAL），复制该文件即完成备份。表分工见 `core/db.py` 的
`SCHEMA` 注释，两条容易踩的建模决定：

- `mod_affix` 按「词缀 × 底材类别」一行（同一词缀在弓上 T5、法杖上 T4 是两行），主键是实质
  字段全集的哈希，**不跨类别去重**；互斥判断用整个 `family_list` 求交集，T 级分组只用
  `correct_group`。
- `pob_mod` 单独建表而不是塞进 `mod_affix`：两源粒度不同，且交叉校验要求各自独立保存，
  混表会让 UPSERT 在冲突被发现前就互相覆盖。对账结果落 `source_conflict`，其中
  `kind=mod` 是已确认冲突（并列展示给用户），`mod_review` 只是待人工核对，**不得当作冲突呈现**。

本机 `data/poe.db` 现状（`GET /healthz` 与 `/admin/stats` 可查）：`entity` 3812、
`mod_affix` 93897、`pob_mod` 25487、`chunk` 17062、`alias` 5407、`game_constant` 499、
`craft_bench_recipe` 783、`pob_base_item` **1022**（18 个装备类别，PoB × POEDB 逐条桥接，
覆盖 PoB 侧 96.1%；未桥接的 41 条已按类别登记在台账）、`source_conflict` 2954。

`craft_metamod_interaction` **134** 行（5 个小节 × 34 行操作，2 个空白格只进审计不成为
证据）。这张表长期是 0 行，而文档一直写着「需联网跑」——**那句是错的**：pipeline 只抓
POEDB 镜像，而镜像页一直在 `data/http_cache` 里、抽取器对它跑得通，这条采集只是从来
没被跑过。教训：「需要联网」这种判断要去看代码抓哪个 URL、缓存里有没有，别照抄注释。

一处数据缺口会直接影响判断：

- **`source_conflict` 里 `kind='mod'`（已确认冲突）是 0 条**，只有 2729 条待人工核对的
  `mod_review`。按需求 15.5，`mod_review` **不得当作冲突呈现**，所以「跨来源冲突并列展示」
  这条能力目前无法验收——缺的是人工核对，不是代码。

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
   安全网是 411 项秒级单测 + `--gate` 离线重放 + 流式批量一致性，重构前后必须持平或变好。
4. **该删就删**：不留「以后可能有用」的函数，不为自己刚写的旧行为留兼容分支；
   约束本身是错的就删约束（`UNIQUE(name_zh)`），别绕开它。删除要证明没丢能力。
5. **防漏改**：成对点清单在规约第 5 条。最高频的是批量 vs 流式、提示词 vs 闸门。
6. **防埋坑**：先量后改，且**两次数字的差不能当因果**（模型有轮次波动，判因要重复运行）；
   先确认测量本身是对的。
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
