# POE1-Agent

面向《流放之路 1》的领域 AI Agent：把 POEDB 与 Path of Building 的数据做成可检索、
**可溯源**的知识库，在此之上回答机制、词缀、装备、制作类问题。

单进程、单 SQLite 文件、零外部服务。不用 Docker，不用向量数据库，不用 Node。

## 第一原则：无据不答

POE1 的机制耦合极深，一个错误的小前提会让整条结论反向。所以本项目**禁止输出经验推断**：
查不到就明确说查不到，并给出缺失的信息点与查证途径，不给一个听起来合理的答案。

拒答粒度是**单条断言**而非整轮回答——有据的照答，无据的逐条列出缺什么。
这条原则落在代码里，不靠提示词祈求：检索侧有充分依据判定，生成侧有断言—引用绑定校验。

完整需求 / 设计 / 任务见 `.kiro/specs/poe1-agent/`。

---

## 当前状态

**已经能用**

| 能力 | 说明 |
| --- | --- |
| 知识库 | POEDB 唯一装备 1341 件、词缀 93875 条（含 T 级 / 物品等级 / 生成权重）、别名 2691 条 |
| PoB 数据源 | 词缀 25487 条、唯一装备 1300 件、游戏常量 499 条；引用钉住 commit |
| 跨来源对账 | 词缀区间一致 86.4%、唯一装备实质字段一致 83.5%，差异登记待核对 |
| 游戏常量 | `calc/constants.py`，逐个常量可溯源到 GGG 原始表；**缺失即抛异常，不给默认值** |
| 混合检索 | 实体精确匹配 + BM25（FTS5 + jieba）+ 词缀意图通道，RRF 融合 + 来源优先级加权 |
| 充分依据判定 | 依据不足时返回 `sufficient=false` 与缺失信息点，**不**进入生成 |
| **问答** | `POST /chat`：强制检索 → 充分性判定 → 生成 → 出口绑定校验 → 装配引用 |
| **出口闸门** | 逐句校验事实断言是否绑定有效证据，未通过由代码拦截；28 项单测覆盖 |
| LLM 客户端 | 直连 LiteLLM 网关，多 Key 故障转移、分级冷却、推理模型适配 |
| 执行轨迹 | 每轮落库，含拦截原因与 Token 明细，`/admin/traces` 可追溯 |

**还没实现**

- 对话式 Web 前端（`static/`，任务 5.1）→ 所以现在只能通过 API 交互
- 流式输出（SSE）与 `POST /v1/chat/completions`。流式必须按句缓冲、校验通过才下发，
  不能把未校验内容先推给用户，需要把出口闸门改造成可增量消费的形式
- 装备文本解析、PoB 码解析、计算器、经济数据（阶段 4 及之后）

也就是说：**问答已经通了，但还没有聊天界面**，交互方式见下。

---

## 快速开始

需要 Python 3.12（最低 3.10）。用 python.org 官方版或 Homebrew 版，
**不要用 macOS 自带的 Python**（它编译时关掉了 SQLite 扩展加载，向量通道会不可用）。

```bash
# 1) 初始化：建 .venv、装依赖、建 data/ 与 logs/、从模板生成 .env
python scripts/setup.py          # Windows: python scripts\setup.py

# 2) 填 .env（至少这两项）
#    LLM_BASE_URL=<你的 LiteLLM 网关地址>
#    LLM_API_KEY=<virtual key>
#    ADMIN_TOKEN=<自己起一个，管理端点用，别和用户 Key 共用>

# 3) 自检：确认 SQLite / FTS5 / 向量扩展 / 凭证配置就绪
.venv/bin/python scripts/selfcheck.py

# 4) 建知识库（首次会抓 POEDB，之后走本地缓存）
.venv/bin/python -m kb.pipeline --category all

# 5) 启动服务
.venv/bin/python main.py
```

Windows 把 `.venv/bin/python` 换成 `.venv\Scripts\python.exe`。
`scripts/setup.sh` 与 `scripts/setup.bat` 只是转调 `setup.py` 的薄封装——
初始化逻辑的唯一真源是 `setup.py`。

启动后监听 `http://127.0.0.1:8787`，**所有路由挂在 `/poe_agent` 前缀下**
（由 `config.yaml` 的 `server.base_path` 配置，置空则不加前缀）。

前缀用显式路由实现，不用 FastAPI 的 `root_path`——后者假定反向代理会剥掉前缀，
本机直连时路径对不上。显式前缀在直连与路径保留型代理（nginx `proxy_pass` 不改 path）
下都成立。

默认**只绑本机**，要对外提供服务请自行加反向代理与 TLS，不要直接把 `server.host`
改成 `0.0.0.0`（会暴露未加固的管理端点）。

单实例保护靠端口绑定失败判断，不用 PID 文件——端口被占用时启动会直接失败退出。
注意进程名被 `setproctitle` 改成了 `poe-agent`，所以 `pkill -f main.py` 找不到它；
要停掉残留实例请按端口找：`lsof -nP -iTCP:8787 -sTCP:LISTEN -t`。

可选：把 PoB 数据也导进来（需要本地有 PathOfBuilding 仓库）

```bash
# 仓库位置按 POB_PATH 环境变量 → config.yaml 的 sources.pob.path
# → 本仓库同级目录 ../PathOfBuilding 依次解析
.venv/bin/python scripts/probe_pob.py --align       # 先清点，确认解析无误
.venv/bin/python scripts/import_pob_constants.py    # 游戏常量（calc/ 的真源）
.venv/bin/python scripts/import_pob_mods.py         # 词缀
.venv/bin/python scripts/import_pob_uniques.py      # 唯一装备（自动过滤历史变体）
.venv/bin/python scripts/verify_pob_mods.py         # 词缀对账
.venv/bin/python scripts/verify_pob_uniques.py      # 唯一装备对账
```

导入唯一装备时会自动过滤历史变体：1300 件里 758 件带 `Variant:` 标记，
不过滤会把旧版数值和现版一起输出（冈姆的壮志的 `+500` 与 `+1000` 并存）。
判据是**排除变体名含 `Pre <版本号>` 的**，而不是「只取 Current」——有些变体
是选择型而非历史版本（`Impresence` 的 5 种元素全都当前有效）。

---

## 通过 web 与 poe-agent 交互

> **所有路径都带 `/poe_agent` 前缀。** 访问不带前缀的 `/docs`、`/healthz` 会得到 404。
> 裸根路径 `http://127.0.0.1:8787/` 会 307 重定向到文档页，忘了前缀时不至于一头雾水。

### 方式一：浏览器打开交互式 API 文档（推荐）

服务起来后直接访问：

```
http://127.0.0.1:8787/poe_agent/docs
```

这是 FastAPI 自带的 Swagger UI，可以在网页上填参数、点 **Try it out** 直接发请求、
看完整响应。目前**这就是最方便的交互方式**——聊天界面还没做。

`http://127.0.0.1:8787/poe_agent/redoc` 是同一套接口的另一种阅读视图。

### 方式二：命令行调接口

**问答** —— 主接口。返回的 `answer` 是**已通过出口绑定校验**的文本。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "猎首（Headhunter）有哪些词缀？"}'
```

真实响应（节选，实测）：

```
## 猎首（Headhunter）词缀
**基础信息**：唯一装备，底材为皮革腰带，需求等级 40[[E1]]。
**显式词缀**
- +(40-55) 力量[[E1]]
- 当你击败稀有怪物时，会获得该怪物属性修正 60 秒[[E1]]
...
> 检索以降级通道完成（alias, fts, mod_intent）：向量检索与重排未启用

引用
[E1] 来源：POEDB → 猎首（Headhunter）
     https://poedb.tw/cn/Headhunter
[E2] 来源：POEDB → 猎首【仿品】（Replica Headhunter）
     https://poedb.tw/cn/Replica_Headhunter
```

句末的 `[[E1]]` 是**证据绑定标记**，不是装饰。出口闸门会逐句检查：

- 事实句必须绑定证据编号，未绑定即拦截
- 句中出现的每个数值必须能在它绑定的证据原文里找到（证据给区间 `(40-55)`
  时，句中取具体值 `47` 算合法）
- 编号必须有效，来源优先级必须能支撑事实断言（P7 社区来源不行）
- 禁止「一般来说」「大概」这类模糊限定语

未通过的句子被替换为拦截说明，并计入响应的 `blocked_claims`。这个字段大于 0
说明模型试图输出无据内容而被挡下，可用 `trace_id` 查具体原因。

响应里的 `model` 字段是实际使用的模型——网关背后挂着多个模型，多 Key 故障转移
可能切到别的模型，而不同模型的推理 token 行为不同。

**依据不足时不会调用 LLM**：

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "流放之路2 的升华机制是什么"}'
```

```
evidence_gap = true
tokens       = {}            ← 完全没调模型
answer       = 知识库中查不到回答这个问题所需的依据，因此不作答……
               **缺失的信息点** / **判定依据** / **建议的查证途径**
citations    = []            ← 不伪造引用
```

这条路径由代码生成，刻意不经模型——它必须完全可预测。

**检索知识库** —— 调试与评测用，返回原始命中。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "猎首", "top_k": 3}'
```

真实响应（节选）：

```
sufficient   = true
reason       = 实体精确命中
channels     = ["alias", "fts"]
top_coverage = 1.0
hits[0]      = [POEDB P1/alias] 猎首（Headhunter）  score=1.0
               https://poedb.tw/cn/Headhunter
```

**先看 `sufficient` 字段。** 它是「无据不答」在检索层的判据：

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "流放之路2 的升华机制是什么"}'
```

```
sufficient    = false
reason        = 仅命中 1 个查询词（下限 2），且无实体精确命中；
                专名查询应由别名表命中，未命中即知识库无此条目
missing_terms = ["升华", "机制"]
```

`sufficient=false` 时 **`hits` 里的内容不得用于支撑事实断言**——BM25 总会返回
「最不差」的若干条，但那不等于有依据。调用方（以及将来的 Agent）必须走缺据声明路径。

自然语言描述也能命中，走的是词缀意图通道：

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "偷取词缀的腰带", "top_k": 3}'
```

可选参数：`top_k`（1–50）、`category`（按一级分类过滤）。

**健康检查** —— 反映 SQLite、向量扩展、已注册工具、LLM 凭证池状态。

```bash
curl -s http://127.0.0.1:8787/poe_agent/healthz
```

```
status = ok
db     = entities 1341 / chunks 25759 / aliases 2691, journal_mode=wal
vector = extension_available true (sqlite-vec v0.1.9), enabled false
llm    = glm-5.2, credentials 1
         pool[0] key=sk-…  healthy=true  cooldown_seconds=0
```

密钥在任何输出里都只出现脱敏形式（`前7位…后4位`）。
`pool` 里能看出哪个 Key 在冷却、因为什么冷却——但注意 `healthy=true` 只表示「不在冷却中」，
建客户端不发请求，所以它不代表这个 Key 真的可用。

**管理端点** —— 采集记录、缺口台账、跨来源差异、执行轨迹。使用独立管理凭证。

```bash
curl -s http://127.0.0.1:8787/poe_agent/admin/stats     -H "Authorization: Bearer $ADMIN_TOKEN"
curl -s http://127.0.0.1:8787/poe_agent/admin/traces    -H "Authorization: Bearer $ADMIN_TOKEN"
curl -s http://127.0.0.1:8787/poe_agent/admin/conflicts -H "Authorization: Bearer $ADMIN_TOKEN"
```

`/admin/trace/{trace_id}` 能看到某一轮的完整轨迹：调了哪些工具、检索到哪些证据、
逐句的绑定校验结果与拦截原因、Token 明细。答案不对时先看这里。

不带凭证返回 **403**；`ADMIN_TOKEN` 未配置时同样返回 403 而不是放行——
没配就默认放行等于把高开销操作公开。

注意 `/admin/conflicts` 里 `kind` 的区别：`mod` 是**已确认冲突**，会按需求 15.5
并列展示给用户；`mod_review` 只是「两源对不上，待人工核对」，不得当作冲突呈现。

### 检索结果里的 `degraded=true` 是什么意思

不是故障。2026-08-04 实测网关未注册任何 embedding / rerank 模型，所以
`config.yaml` 里 `retrieval.vector.enabled` 与 `rerank.enabled` 都是 `false`，
检索走 BM25 + 实体精确 + 词缀意图三路，`degraded` 标记的是「向量通道未参与」。
将来运维在网关上注册了 embedding 模型，只改配置即可启用，代码无需改动。

---

## 数据源与优先级

优先级内建于检索排序，不是提示词里的建议。

| 级别 | 来源 | 说明 |
| --- | --- | --- |
| P1 | POEDB | 中文名与页面说明的真源，最高优先级；冲突时推荐采用 |
| P2 | Path of Building `src/Data/` | 数值与结构的真源（GGG 客户端 `.dat` 机器导出），无中文名 |
| P3 | PoE Wiki | 尚未接入 |
| P4 | Craft of Exile | 尚未接入 |
| P5 | poe.watch 等经济源 | 尚未接入 |
| P6 | GGG 官方 Patch Notes | 尚未接入 |
| P7 | Reddit 等社区讨论 | **不得**支撑事实断言，仅可标注为社区观点用于建议 |

PoB 排 P2 而不是 P1：它在数值上更接近一手，但不含中文名与页面说明，
且社区分支存在人工修补。引用 PoB 时 URL 钉住 commit，例如
`.../blob/9e6ea223e.../src/Data/ModExplicit.lua`——指向可变分支的引用不可复现。

**采集合规**：只抓公开页面、遵守 robots.txt、令牌桶限速、磁盘缓存 + 内容哈希增量、
携带可识别 User-Agent、输出中链回原站。不做任何游戏客户端注入、内存读取或自动交易。

---

## 常用脚本

| 脚本 | 用途 |
| --- | --- |
| `scripts/setup.py` | 环境初始化（跨平台唯一真源） |
| `scripts/selfcheck.py` | 配置 / 数据库 / FTS5 / 向量扩展 / 凭证 体检 |
| `scripts/test_search.py` | 检索链路验证 |
| `scripts/verify_mods.py` | POEDB 词缀自检：模板往返命中率、T 级不变量 |
| `scripts/verify_uniques.py` | 唯一装备抽取抽样核对 |
| `scripts/probe_gateway.py` | LLM 网关能力探测（模型清单 / tool calling / 流式 / 推理特征） |
| `scripts/verify_llm.py` | LLM 客户端验证（`--offline` 无需凭证） |
| `scripts/probe_pob.py` | PoB 数据资产清点与解析可行性验证 |
| `scripts/import_pob_mods.py` | 导入 PoB 词缀（`--dry-run` 只体检） |
| `scripts/import_pob_constants.py` | 导入游戏常量（`calc/` 真源） |
| `scripts/import_pob_uniques.py` | 导入 PoB 唯一装备（过滤历史变体） |
| `scripts/verify_pob_mods.py` | POEDB × PoB 词缀对账 |
| `scripts/verify_pob_uniques.py` | POEDB × PoB 唯一装备对账 |
| `scripts/verify_chat.py` | `/chat` 端到端验证（`--gap-only` 不需要凭证） |
| `scripts/check_spec_refs.py` | 校验 spec 里的需求交叉引用 |

单元测试用标准库 `unittest`，不引入 pytest：

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

目前 42 项：28 项覆盖出口绑定闸门（「该拦的拦住」与「该放的放行」成对，
错误拒答与无据漏出要一起压制，见需求 16.7），14 项覆盖游戏常量访问层
（重点是「缺失即抛异常，绝不给默认值」）。

---

## 目录结构

仓库根平铺，不做 `src/` 包布局、不打包安装，以仓库根为工作目录运行。

```
main.py                 入口：装配 FastAPI + 进程内启动 uvicorn
core/                   配置、SQLite、日志、LLM 客户端
api/                    路由层：只做校验与编排调用，不写业务逻辑
                          chat.py / search.py / admin.py / deps.py（鉴权）
orchestrator/           主循环、出口绑定闸门、引用装配
                          loop.py / binding.py / citation.py
agents/                 领域 Agent：提示词 + 允许工具集 + 输出契约
tools/                  工具注册与实现（kb_search / entity_lookup）
calc/                   常量访问层与计算器（constants.py 缺失即抛异常）
entities/               实体模型、分类体系、词缀反查
kb/                     采集管线与各来源抽取器（poedb / pob）
retrieval/              FTS5、别名、混合检索融合、分词、领域词表
tests/unit/             单元测试（unittest）
scripts/                初始化、体检、验证、导入
config.yaml             声明式行为：路由前缀、检索参数、来源优先级、领域词表
.env                    密钥（不入版本库）
data/                   SQLite 单文件 + HTTP 缓存（不入版本库）
logs/                   按日期命名的日志（不入版本库）
```

全部状态集中在 `data/poe.db` 一个文件里，复制该文件即完成备份。

---

## 已知限制

- **没有聊天界面，也没有流式输出。** 问答只能通过 API 调用。流式要求按句缓冲、
  校验通过才下发，需要把出口闸门改造成可增量消费的形式，与前端一起做。
- **`.env` 里的 `LLM_API_KEY` 需要填真值。** 模板里是占位符 `sk-`；不填的话
  有据路径会返回「生成失败」，但缺据路径仍能正常工作（它不调模型）。
- **出口闸门的事实句判定是规则驱动的。** 判不出类型时默认按事实句处理（要求绑定
  引用），这是刻意选的安全侧：漏出无据断言的代价高于多拦一句过渡语。代价是偶尔会
  误拦无害的连接句。Markdown 标题、加粗标签、分隔线、缺据声明句已豁免。
- **计算派生尚未强校验。** 派生句的结论数值本来就不在证据里，目前只要求整个回答里
  有可作为推导输入的有据事实句。`calc/` 就位后应改为由计算器产出并回填校验。
- **向量通道关闭。** 网关没有 embedding 模型，描述性查询依赖 `search_keywords` 的覆盖质量。
- **混合词缀反查失配。** 带 ` / ` 的多行词缀（如「+X% 全抗 / 召唤生物 +Y% 抗性」）
  在 `mod_lookup` 里仍按单行模板匹配，会失配。
- **底材 / 技能 / 天赋树尚未入库。** PoB 有完整数据但无中文名，需先补 POEDB 对应中文页，
  否则 `name_zh` 只能填英文，违背中文输出要求。
- **PoB 唯一装备的历史变体已过滤但未入库。** 2157 条被排除的历史词缀目前丢弃了。
  存下来能回答版本改动类问题，但必须与当前数据严格隔离，否则会被当成现值输出。
- **POEDB 侧 76% 的唯一装备缺需求等级。** 交叉校验发现的采集缺口（926/1215），
  PoB 侧有值可回填，装备分析需要这个字段。
- **唯一装备的 `mod_count` 两源只有 52.9% 一致。** 这是建模差异不是数值分歧：
  PoB 把 `Elder Item` 这类影响力标记也算独立词缀，POEDB 不抽取。已降级为结构指标。

---

## 授权与致谢

游戏数据版权归 Grinding Gear Games。
Path of Building Community 以 MIT 授权，其 `src/Data/` 内的游戏数据同样版权归 GGG。
本项目仅在本地使用这些数据，并在输出中链回原始站点。
