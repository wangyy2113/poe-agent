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
| PoB 数据源 | 词缀 25487 条，含按底材标签的权重向量与交易站 stat id；引用钉住 commit |
| 跨来源对账 | 两源词缀区间数值一致 22848 行（86.4%），差异登记待核对 |
| 混合检索 | 实体精确匹配 + BM25（FTS5 + jieba）+ 词缀意图通道，RRF 融合 + 来源优先级加权 |
| 充分依据判定 | 依据不足时返回 `sufficient=false` 与缺失信息点，**不**进入生成 |
| HTTP 服务 | `GET /healthz`、`POST /search`、`GET /admin/stats`（独立管理凭证） |
| LLM 客户端 | 直连 LiteLLM 网关，多 Key 故障转移、分级冷却、推理模型适配 |

**还没实现**

- 对话式 Chat Web 前端（`static/`，任务 5.1）→ 所以 `GET /` 现在返回 404
- `POST /chat` 与 `POST /v1/chat/completions` 流式对话接口（任务 3.8）
- Orchestrator 主循环、Agent、工具注册（阶段 3）
- 装备文本解析、PoB 码解析、计算器、经济数据（阶段 4 及之后）

也就是说：**现在能通过 web 做的是「检索」，还不是「对话」。** 下面的交互方式据此写。

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

启动后监听 `http://127.0.0.1:8787`。默认**只绑本机**，要对外提供服务请自行加反向代理与 TLS，
不要直接把 `config.yaml` 的 `server.host` 改成 `0.0.0.0`（会暴露未加固的管理端点）。

单实例保护靠端口绑定失败判断，不用 PID 文件——端口被占用时启动会直接失败退出。

可选：把 PoB 数据也导进来（需要本地有 PathOfBuilding 仓库）

```bash
# 仓库位置按 POB_PATH 环境变量 → config.yaml 的 sources.pob.path
# → 本仓库同级目录 ../PathOfBuilding 依次解析
.venv/bin/python scripts/probe_pob.py --align      # 先清点，确认解析无误
.venv/bin/python scripts/import_pob_mods.py        # 导入词缀
.venv/bin/python scripts/verify_pob_mods.py        # 与 POEDB 对账
```

---

## 通过 web 与 poe-agent 交互

### 方式一：浏览器打开交互式 API 文档（推荐）

服务起来后直接访问：

```
http://127.0.0.1:8787/docs
```

这是 FastAPI 自带的 Swagger UI，可以在网页上填参数、点 **Try it out** 直接发请求、
看完整响应。目前**这就是最方便的 web 交互方式**——对话式界面还没做。

`http://127.0.0.1:8787/redoc` 是同一套接口的另一种阅读视图。

> `http://127.0.0.1:8787/` 返回 404 是正常的：`static/` 目录还不存在，
> FastAPI 只在该目录存在时才挂载静态前端（见 `main.py` 的 `create_app`）。

### 方式二：命令行调接口

**检索知识库** —— 核心接口。返回命中的知识切片、来源、优先级与可点击 URL。

```bash
curl -s -X POST http://127.0.0.1:8787/search \
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

**先看 `sufficient` 字段。** 它是「无据不答」在接口层的出口：

```bash
curl -s -X POST http://127.0.0.1:8787/search \
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
curl -s -X POST http://127.0.0.1:8787/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "偷取词缀的腰带", "top_k": 3}'
```

可选参数：`top_k`（1–50）、`category`（按一级分类过滤）。

**健康检查** —— 反映 SQLite、向量扩展、LLM 凭证池状态。

```bash
curl -s http://127.0.0.1:8787/healthz
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

**管理端点** —— 采集任务记录与缺口台账。使用独立管理凭证。

```bash
curl -s http://127.0.0.1:8787/admin/stats \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

不带凭证返回 **403**；`ADMIN_TOKEN` 未配置时同样返回 403 而不是放行——
没配就默认放行等于把高开销操作公开。

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
| `scripts/verify_pob_mods.py` | POEDB × PoB 词缀对账 |
| `scripts/check_spec_refs.py` | 校验 spec 里的需求交叉引用 |

---

## 目录结构

仓库根平铺，不做 `src/` 包布局、不打包安装，以仓库根为工作目录运行。

```
main.py                 入口：装配 FastAPI + 进程内启动 uvicorn
core/                   配置、SQLite、日志、LLM 客户端
entities/               实体模型、分类体系、词缀反查
kb/                     采集管线与各来源抽取器（poedb / pob）
retrieval/              FTS5、别名、混合检索融合、分词、领域词表
scripts/                初始化、体检、验证、导入
config.yaml             声明式行为：检索参数、来源优先级、限速、领域词表
.env                    密钥（不入版本库）
data/                   SQLite 单文件 + HTTP 缓存（不入版本库）
logs/                   按日期命名的日志（不入版本库）
```

全部状态集中在 `data/poe.db` 一个文件里，复制该文件即完成备份。

---

## 已知限制

- **对话能力尚未接通。** 知识库与检索已就绪，Orchestrator / Agent / Chat 接口还没写，
  所以现在只能检索，不能问答。
- **向量通道关闭。** 网关没有 embedding 模型，描述性查询依赖 `search_keywords` 的覆盖质量。
- **混合词缀反查失配。** 带 ` / ` 的多行词缀（如「+X% 全抗 / 召唤生物 +Y% 抗性」）
  在 `mod_lookup` 里仍按单行模板匹配，会失配。
- **底材 / 技能 / 天赋树尚未入库。** PoB 有完整数据但无中文名，需先补 POEDB 对应中文页，
  否则 `name_zh` 只能填英文，违背中文输出要求。
- **PoB 唯一装备的历史变体尚未处理。** 1293 件里 752 件带 `Variant:` 标记，
  必须只取 `Current`，否则会把旧版数值一起输出。这是导入前必须先做的。

---

## 授权与致谢

游戏数据版权归 Grinding Gear Games。
Path of Building Community 以 MIT 授权，其 `src/Data/` 内的游戏数据同样版权归 GGG。
本项目仅在本地使用这些数据，并在输出中链回原始站点。
