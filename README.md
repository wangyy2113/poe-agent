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
| 知识库 | POEDB 唯一装备 1342 件、词缀 93897 条（含 T 级 / 物品等级 / 生成权重）、别名 8106 条 |
| PoB 数据源 | 词缀 25487 条、唯一装备 1300 件、游戏常量 543 条；引用钉住 commit |
| 技能石与辅助宝石 | 810 颗（548 主动 + 262 辅助）+ 30422 行每级字面值；PoB 出结构、POEDB 出中文名，按 `grantedEffectId` 桥接。**每级基础伤害已入库**（175 颗技能 7000 行，按 PoB 的 effectiveness 公式算出显示值，与 POEDB 技能页对账 20/20）；保留百分比与定值保留都已入库；**属性需求与获取途径仍未采集**，DPS 也仍不算（要攻速与整条加成链） |
| 通货 | 113 条（中英文名、堆叠上限、效果原文）；工艺台费用引用的 18 个通货全部能对上；**获取途径、兑换比例与价格未采集** |
| 圣甲虫 | 118 个（效果、堆叠上限、地图装置放置上限）；**掉落来源与价格未采集** |
| 地图 | 159 张（等阶、头目、额外目标、命运卡、工艺台配方、图鉴相邻地图）；**地图词缀与内部布局未采集** |
| 星团珠宝 | 3 个尺寸（小 2–3 / 中 4–6 / 大 8–12 个小天赋）+ 55 条小天赋类型（仅英文）+ 308 条核心天赋与基石清单；**哪种类型能开出哪些核心天赋未采集** |
| 天赋树路径 | 连线 3382 条 + 7 个职业起点入库；`passive_path` 按职业算「点到某节点要几点」并给出逐节点路径（总点数 123） |
| 怪物数据 | 怪物词缀 183 条（有中文文本的）+ 可作幽魂怪物 1948 个与其伤害% / 生命%，其中 268 个另有 PoB 的四抗性与建模系数；倍率排名在这 268 个里做；**Boss 技能与打法、掉落、怪物技能说明未采集** |
| 跨来源冲突 | 幽魂对账给出 8 条已确认分歧 → 答案里并列双方并推荐 POEDB（需求 15.5 首次可验收）；43 条粒度差异单列，不当冲突呈现 |
| 元词缀矩阵 | 页面矩阵 134 条（operation × guard，5 个小节）；空白格只进审计、不成为证据 |
| 跨来源对账 | 词缀区间一致 86.4%、唯一装备实质字段一致 83.5%，差异登记待核对 |
| 游戏常量 | `calc/constants.py`，逐个常量可溯源到 GGG 原始表；**缺失即抛异常，不给默认值** |
| 受击结算 | `calc/mitigation.py` + `mitigation_calc` 工具：闪避与命中率（怪物命中值取自库里的等级表）、格挡几率与上限、法术压制（减 **40%** 不是 50%）、护甲对某一击的减伤（**护甲不是固定百分比减伤**，不给伤害数值就没有答案）。**不含头目的护甲/闪避倍率** |
| 暴击 | `calc/crit.py` + `crit_calc` 工具：暴击率（基础 + 定值）×(1 + 增加)× 更多、夹在 **100%** 上限内（不是 95%），以及平均伤害倍率 =(1 − 暴击率)+ 暴击率 × 暴击加成。基础暴击按技能名或武器底材名查库。**只给倍率不给 DPS**（伤害数值未采集） |
| 一发最大承受 | `calc/maxhit.py` + `max_hit_calc` 工具：把抗性 → 护甲/减伤 → 生命+护盾池反解，给出「一发最多能挨多少」与「相当于裸池的多少倍」。无护甲是一步除法，有护甲按 PoB 的二次方程闭式解。验证方式是**自洽回代**（算出来的值走正向链路必须正好打满池子）。**只算单一伤害类型、不转化**，不含守护技能 / Ward / Mind over Matter |
| 门槛型异常 | `calc/threshold.py` + `ailment_threshold_calc` 工具：感电 / 冰缓 / 焦灼 / 脆弱 / 干枯的强度 = 基数 ×(伤害 ÷ 异常门槛)^0.4；门槛按等级表 × 头目 7.68 × 地图倍率算（83 级小怪 32,017、头目 1,086,003）。也能反问「要感电到 X% 需要多大伤害」。**冰冻只给门槛判据、不算时长曲线** |
| 持续伤害 | `calc/ailment.py` + `ailment_dot_calc` 工具：点燃 / 中毒 / 流血的**每秒**伤害、持续与总伤害（点燃每秒 90%×4 秒 = 总共 360%），含叠加规则（中毒无上限）与减伤口径（**流血不吃护甲**）。**感电与冰冻是门槛型异常、不算** |
| 保留计算 | `calc/reservation.py` + `reservation_calc` 工具：按名字查库取每个光环的基础保留（**含定值保留**，第二十四轮补上的 400 行），套 PoB 的取整规则（各自 round 2 位 → 求和 → 只向上取整一次）与保留效率（除法），给出合计与剩余。**不含血魔法** |
| 回复计算 | `calc/recovery.py` + `recovery_calc` 工具：护盾回充（每秒量、**2 秒延迟**、多久回满）、偷取（单次量、持续时间、速率上限与**单次实例上限**两层）、魔力再生（基数每分钟 105%）。**不含击中回复 / 药剂 / Mind over Matter** |
| 抗性计算 | `calc/resistance.py` + `resistance_calc` 工具：按 PoB 原式复算（加总 → 截断小数 → 夹在 −200% 与最大抗性之间），给出**逐步推导**、超出上限的部分、距上限差多少与承受倍率；与 PoB 自己的测试期望值对账，公式原文钉住上游行号。**图腾抗性与持续伤害抗性未实现**，也不读取角色档案 |
| 混合检索 | 实体精确匹配 + BM25（FTS5 + jieba）+ 词缀意图通道，RRF 融合 + 来源优先级加权 |
| 充分依据判定 | 依据不足时返回 `sufficient=false` 与缺失信息点，**不**进入生成 |
| **问答** | `POST /chat`：强制检索 → 充分性判定 → 生成 → 出口绑定校验 → 装配引用 |
| **流式问答** | `POST /chat/stream`：SSE，**按句**校验通过才下发，未校验内容不外发 |
| **出口闸门** | 逐句校验事实断言是否绑定有效证据，未通过由代码拦截；31 项单测覆盖 |
| **聊天界面** | `static/` 单页前端，无构建步骤、无 Node、无第三方 JS |
| **装备解析** | 游戏内 Ctrl+C / Ctrl+Alt+C 文本 → 结构化装备；中英文都认 |
| **词缀 T 级** | 按底材类别 + 数值区间定档；查不到就标「未匹配」并说明原因 |
| **装备分析** | `POST /item`：基础信息、词缀档位、可制作方向，全程不调 LLM |
| LLM 客户端 | 直连 LiteLLM 网关，多 Key 故障转移、分级冷却、推理模型适配 |
| 缺口台账 | `knowledge_gap` 按频次排序即采集优先级；**评测流量与永久越界（POE2）不计入**（否则前两名会是本该拒答的黄金用例），`/admin/stats` 同时列出被排除的条目数 |
| 执行轨迹 | 每轮落库，含拦截原因与 Token 明细，`/admin/traces` 可追溯 |

**还没实现**

- `POST /v1/chat/completions`（OpenAI 兼容端点）
- PoB 码解析（前端能识别出来，但明说未实现，排在 V2）
- 市场价值与经济数据（所以装备分析里「值不值得裂界」一律输出「数据不足」）
- 会话重命名、图片上传
- 计算器的其余部分（抗性、受击结算、回复、保留、持续伤害已实现；命中 / DPS（要先有 effectiveness 公式与对账口径）、伤害转化下的 Max Hit、冰冻时长曲线还没有）

也就是说：**打开浏览器就能聊，也能直接把装备贴进去**，交互方式见下。

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
#    可单独跑：unique_item / unique_l2 / mods / bench（工艺台目录）
#              / metamods（元词缀矩阵）/ tree（天赋树与 Atlas）/ coverage（只刷覆盖声明）

# 5) 启动服务
.venv/bin/python main.py
```

Windows 把 `.venv/bin/python` 换成 `.venv\Scripts\python.exe`。
`scripts/setup.sh` 与 `scripts/setup.bat` 只是转调 `setup.py` 的薄封装——
初始化逻辑的唯一真源是 `setup.py`。

启动后监听 `http://127.0.0.1:8787`，**所有路由挂在 `/poe_agent` 前缀下**
（由 `config.yaml` 的 `server.base_path` 配置，置空则不加前缀）。
浏览器打开 <http://127.0.0.1:8787/poe_agent/> 就是聊天界面。

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
> 裸根路径 `http://127.0.0.1:8787/` 会 307 重定向到前端，忘了前缀时不至于一头雾水。

### 方式一：聊天界面（推荐）

服务起来后直接访问：

```
http://127.0.0.1:8787/poe_agent/
```

单页静态前端，`static/` 下三个文件，由 FastAPI `StaticFiles` 托管。
**没有构建步骤、没有 Node 运行时、没有第三方 JS**——Markdown 渲染器是自写的，
因为答案的 Markdown 形态有限，而自己写才能强制「先转义再解析」。
自动跟随系统深色模式，移动端可用。

界面上能看到的东西，每一样都对应一条硬规则：

- **句子是逐句冒出来的**，不是逐字。因为校验单位是句子——一句话是不是事实断言、
  绑定编号有效不有效、句里的数值能不能溯源，都得等这句说完才能判。逐字推送等于
  把未校验内容先给了用户，之后撤不回来。打字机效果是前端补的观感，不是真的逐字流
- **正文出现前会显示「思考中」**。`glm-5.2` 是推理模型，推理 token 能占输出的 98%，
  这段时间正文一个字都没有。推理内容**绝不外发**——模型正是在推理里动用参数记忆做
  猜测的，而那恰好是「无据不答」要拦的东西，透出去等于绕过全部闸门
- **句末的上标数字**是证据角标，点一下跳到底部来源列表并高亮，来源条目带优先级
  标签（P1 POEDB / P2 PoB / …），一眼看出这句话的证据有多硬
- **琥珀色分区**是缺据声明：查不到什么、判定依据、去哪查。它由代码生成、不经模型，
  必须完全可预测
- **被拦截的句子就地留提示**，不堆到末尾——堆末尾用户看不出是哪句被拦，等于没告知

**把装备贴进输入框**会触发识别（需求 6.3/6.4）：认出装备文本或 PoB 码时先弹一张
卡片让你确认，不直接分析。两个理由——识别可能判错，判错了直接分析等于你莫名收到
一份不相干的报告；而且你粘贴的可能只是问题的一部分（贴完装备还想补一句「值多少钱」），
直接触发就把下文吞了。PoB 码能识别但会明说尚未实现。

左上角可切换历史会话（存 `localStorage`，标题取首条用户消息），点「新对话」重开。

### 方式二：浏览器打开交互式 API 文档

```
http://127.0.0.1:8787/poe_agent/docs
```

FastAPI 自带的 Swagger UI，可以填参数、点 **Try it out** 直接发请求、看完整响应。
调 `/search`、`/admin/*` 这类前端没暴露的接口时用它最省事。

`http://127.0.0.1:8787/poe_agent/redoc` 是同一套接口的另一种阅读视图。

### 方式三：命令行调接口

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

**流式问答** —— `POST /chat/stream`，SSE。前端用的就是它。

```bash
curl -N -X POST http://127.0.0.1:8787/poe_agent/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"message": "猎首（Headhunter）有哪些词缀？"}'
```

每帧形如 `data: {json}`，用内嵌的 `type` 字段区分事件，**不用** SSE 的 `event:` 字段——
让前端一套解析逻辑处理完。

| `type` | 时机与载荷 |
| --- | --- |
| `meta` | 开头一帧，给 `trace_id` 与 `conversation_id` |
| `thinking` | 模型开始输出推理内容。**只是信号，不含推理正文** |
| `tool` | 调用了某个工具，带 `name` |
| `sentence` | 一句**已通过校验**的正文，带 `kind`（事实/派生/建议）与 `labels`（证据编号） |
| `blocked` | 一句未通过校验，带**替换文本**与 `reason`；原文是无据断言，不外发 |
| `gap` | 依据不足，整轮走缺据声明；这一帧之后直接 `done` |
| `citations` | 引用列表，在正文之后 |
| `note` | 检索降级说明 |
| `error` | 生成失败，带脱敏后的原因 |
| `done` | 收尾，给 `blocked_claims`、`model`、`tokens`、`elapsed_ms` |

两条典型帧序列（`sentence` 帧数随回答长度变化，「猎首有哪些词缀」几次实测在 10–12 句）：

```
有据：meta → thinking → sentence × N → citations → note → done
      句被拦时中间夹 blocked 帧，正文位置照留，不挪到末尾
缺据：meta → gap → done                       （3 帧，tokens 为空，不调模型）
```

**流式的粒度是句，不是字，这是设计约束不是偷懒。** 逐字推送等于把未校验内容先给用户，
之后没法撤回；而一句话是不是事实断言、绑定编号有效不有效、句里的数值能不能在证据原文
里找到，都得等这句说完才能判。

增量校验由 `orchestrator/binding.StreamingGate` 做，它**复用批量 `verify()` 的分句与
判定函数**，不是另写一套增量规则。两份规则迟早分叉，届时同一个回答走流式和非流式会得到
不同结果，哪个对都说不清。单测把这条钉住（`tests/unit/test_streaming_gate.py`）：
10 组文本 × 4 种切片粒度，拦截数、分类、输出三项必须与批量校验完全一致。

**装备分析** —— `POST /item`，把游戏内复制的装备文本变成结构化报告。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/item \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{"text": "物品类别: 项链\n稀有度: 稀有\n恐惧之瞳\n玛瑙护身符\n--------\n物品等级: 84\n--------\n+107 最大生命\n+43% 火焰抗性"}
JSON
```

真实输出（节选，实测）：

```
- `+107 最大生命` — **T2/9**[[E1]]
  - 前缀；Base；区间 100-114；roll 50%；物品等级 44；生成权重 1000
  - T 级来源：POEDB 词缀库
- `+43% 火焰抗性` — **T2/8**[[E1]]
  - 后缀；Base；区间 42-45；roll 33%；物品等级 72；生成权重 1000

可制作方向
- `+107 最大生命`
  - 该底材上最高档位是 T1（115-129），要求物品等级 54[[E1]]
  - 互斥：+(41-45) 最大生命 / 每秒再生 4 魔力（工艺工作台 T1/6）

市场价值
**无价格数据。** 经济数据源尚未接入……**不按同类物品类推价格**
```

**这条链路全程不调用 LLM**，所以输出完全可预测：同一份文本永远得到同一份报告，
数值只可能来自装备文本本身或词缀库的某一行。也因此它不需要过出口绑定闸门——
闸门防的是模型编造，这里没有模型。

关于 T 级，有三件事值得说明：

- **`tier_source` 要跟 `tier` 一起看。** 四种取值：`item_text` 是游戏原文给的
  （用 **Ctrl+Alt+C 进阶复制**时游戏会自己带上 `(Tier: 5)` 与词缀区间，那比查我们
  抓来的库更硬，而且语言无关）；`poedb` 是中文文本查库；`bridge` 是英文文本经
  「PoB 阶梯 ⊇ POEDB 阶梯」的指纹比对得出；空串表示**没有 T 级**，此时 `reason`
  说明为什么。
- **查不到就标「未匹配」，不给推测值**（需求 7.9）。五种情况各给不同原因：类别在
  词缀库里无对应页面、库里没这条词缀、数值不落在任何档位（可能被品质或工艺台改过）、
  同类别下不同底材页面给出不同档位、隐式词缀来自尚未入库的底材。
- **不给市场价格。** 经济数据源还没接，所以「是否值得裂界 / 镜像湖 / 够不够镜子级别」
  三项一律输出「数据不足，不作判断」+ 缺哪项数据，同时把已知事实（档位、roll
  百分比、腐化状态）摆出来交给你判断。给一条「建议裂界」会让人真去花分裂石，
  而我们连该词缀裂界后值多少都查不到。

格式认不出时返回 **422**，带失败原因与期望格式样例，不返回半个解析结果：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8787/poe_agent/item \
  -H 'Content-Type: application/json' -d '{"text": "今天天气不错"}'
# 422
```

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
| `scripts/verify_chat.py` | `/chat`、`/chat/stream`、`/item` 端到端验证，58 项（`--gap-only` 31 项，不需要凭证） |
| `scripts/check_spec_refs.py` | 校验 spec 里的需求交叉引用 |
| `scripts/check_doc_facts.py` | 核对文档里的数字断言（单测项数、库内行数）与实际状态是否一致 |

| `scripts/eval_run.py` | 黄金问答集评测：检索侧指标 / 录制回放 / 出口闸门指标 / 人工审计清单 |
| `scripts/import_pob_bases.py` | 导入 PoB 底材注册表，18 个装备类别（默认不联网；`--dry-run` 清点、`--category` 单类、`--fetch` 缺页补抓） |

### 评测：四项指标与黄金问答集

`config.yaml` 里的阈值与 `orchestrator/binding.py` 的闸门规则都需要判据，否则改动之后
无法判断是变好还是变坏。判据来自 `tests/golden/questions.yaml`（109 条，六类样本：
专名 / 描述性 / 数值 / 跨来源 / **确定不覆盖** / 工具路由），基线与结论记在
`tests/golden/BASELINE.md`。

```bash
# 检索侧指标，全离线（只读 data/poe.db，不调模型）
.venv/bin/python scripts/eval_run.py
.venv/bin/python scripts/eval_run.py --sweep 0.3,0.5,0.7   # 扫 min_coverage 看成对代价

# 录一批真实响应（需要 LLM 凭证，会消耗 token），之后离线重放
.venv/bin/python scripts/eval_run.py --record
.venv/bin/python scripts/eval_run.py --gate

# 生成人工审计清单，结论回填 tests/golden/audit.yaml
.venv/bin/python scripts/eval_run.py --audit
```

**录制回放而不是每次联网**：录一次真实响应存进 `tests/golden/recordings/`，之后重放
就能回归出口闸门的行为。录制里存的是**未过闸门的模型原文**（待检材料，不是答案），
并按 `chunk_id` 逐条校验证据正文是否仍然一致——变了就报「录制过期需重录」，
绝不静默给出错数字。

`scripts/test_search.py` 现在也读同一份 `questions.yaml`（原先内嵌了第二份用例，
两份迟早分叉）。

单元测试用标准库 `unittest`，不引入 pytest：

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

目前 1014 项，其中与「无据不答」直接相关的：

| 组 | 项数 | 重点 |
| --- | --- | --- |
| 出口绑定闸门 | 31 | 「该拦的拦住」与「该放的放行」成对——错误拒答与无据漏出要一起压制（需求 16.7） |
| 装备文本解析 | 45 | 真实 Ctrl+C 文本 fixture；「普通复制不许带 T 级」「认不出必须显式失败」 |
| 词缀 T 级匹配 | 26 | 内存桩库，数值照抄真实库；「匹配不上必须标未匹配」 |
| 装备分析报告 | 15 | 「不给价格」「三项价值判断都不给结论」「最高档位不跨来源类别」 |
| 游戏常量访问 | 14 | 缺失即抛异常，绝不给默认值 |
| 流式闸门一致性 | 9 | 流式与批量校验的判定必须完全一致 |
| 表格断言单位 | 10 | 表格整块算一个断言；绑定位置放宽但「要不要绑定」没放宽；表格未收齐不下发 |
| 维度覆盖闸门 | 14 | 实体在库里 ≠ 被问的维度有数据；复合问题不整轮拒答 |
| 缺口台账 | 10 | 排版元素不入账、归一化合并、三类来源分开 |
| 黄金问答集自检 | 17 | 评测集本身写错会给出「看起来正常的错数字」，比报错危险 |

装备相关的测试全部离线：解析器不连库，T 级匹配用内存 SQLite 打桩。
fixture 来源逐个标注在 `tests/unit/fixtures/items/README.md`——样本的可信度决定
测试的可信度，所以那里不放来源不明的样本。

---

## 目录结构

仓库根平铺，不做 `src/` 包布局、不打包安装，以仓库根为工作目录运行。

```
main.py                 入口：装配 FastAPI + 进程内启动 uvicorn
core/                   配置、SQLite、日志、LLM 客户端
api/                    路由层：只做校验与编排调用，不写业务逻辑
                          chat.py / search.py / item.py / admin.py / deps.py（鉴权）
orchestrator/           主循环、出口绑定闸门、引用装配
                          loop.py / binding.py / citation.py
agents/                 领域 Agent：提示词 + 允许工具集 + 输出契约
tools/                  工具注册与实现。顺序即推荐优先级：
                          affix_pool_odds / check_mod_combination / bench_recipe_catalog
                          / metamod_interaction_matrix / mod_tiers / skill_gem_levels
                          / spectre_stats / passive_path / cluster_jewel_info
                          / resistance_calc / mitigation_calc / recovery_calc / reservation_calc
                          / ailment_dot_calc
                          / ailment_threshold_calc
                          / max_hit_calc / crit_calc
                          / stat_sources
                          / entity_lookup / kb_coverage / kb_search（兜底）
craft/                  制作分析：feasibility.py 组合三态可行性 / pool.py 胸甲条件权重概率
calc/                   常量访问层与计算器（constants.py 缺失即抛异常 / derive.py 推导件
                          / resistance.py 抗性
                          / mitigation.py 受击结算
                          / recovery.py 回复
                          / reservation.py 保留
                          / ailment.py 持续伤害
                          / threshold.py 门槛型异常
                          / maxhit.py 一发最大承受 / crit.py 暴击）
parsers/                输入解析：装备文本、词缀档位匹配、分析报告
                          item_text.py（纯文本，不连库）/ item_mods.py / mod_bridge.py / item_report.py
entities/               实体模型、分类体系、词缀反查
kb/                     采集管线与各来源抽取器（poedb / pob）
retrieval/              FTS5、别名、混合检索融合、分词、领域词表
static/                 单页前端：index.html / app.js / style.css（无构建步骤）
tests/unit/             单元测试（unittest）
tests/golden/           黄金问答集、录制回放、人工审计结论、指标基线
scripts/                初始化、体检、验证、导入、评测
config.yaml             声明式行为：路由前缀、检索参数、来源优先级、领域词表
.env                    密钥（不入版本库）
data/                   SQLite 单文件 + HTTP 缓存（不入版本库）
logs/                   按日期命名的日志（不入版本库）
```

全部状态集中在 `data/poe.db` 一个文件里，复制该文件即完成备份。

---

## 已知限制

- **装备文本的中文表头标签未经真机核实。** 英文格式有 PoB 的解析器实现作依据
  （`src/Classes/Item.lua`），中文客户端 Ctrl+C 的表头标签则没有可核实来源——
  POEDB 缓存页里只有词缀文本，没有物品提示框的标签行。代码结构保证「标签认错只会
  让该字段留空并进 `warnings`，不会静默错解析」，但要提高可信度得拿一份真机中文
  复制文本校对 `parsers/item_text.py` 的 `_ZH_KEYS`。
- **英文普通复制的 T 级靠桥接，覆盖不全。** POEDB 只有中文词缀文本，PoB 有英文但
  没有 tier 列，所以英文走「PoB 阶梯 ⊇ POEDB 阶梯」的指纹比对。实测弓 5/5、
  手套 5/6，但特殊来源词缀（天灾、附魔、影响词）大多桥不过去。**用 Ctrl+Alt+C
  进阶复制可以绕开这个限制**——游戏会自己带上 T 级。
- **隐式词缀拿不到 T 级与数值范围。** 底材库已入库（18 类 1022 条，含**英文**隐式词缀原文），
  但 T 级与数值区间在 POEDB 的底材**详情页**上，类别列表页没有；中文隐式文本同理。
  所以装备分析里的隐式词缀仍只能标「未匹配」。
- **PoB 码只能识别不能解析。** 前端认得出来并明说未实现，排在 V2。
- **会话历史只存在浏览器 `localStorage` 里**（上限 30 条）。换浏览器或清缓存就看不到
  列表了——消息本身在 `data/poe.db` 里，但没有按用户维度检索会话的接口。
  会话重命名也还没做。
- **`.env` 里的 `LLM_API_KEY` 需要填真值。** 模板里是占位符 `sk-`；不填的话
  有据路径会返回「生成失败」，但缺据路径仍能正常工作（它不调模型）。
- **出口闸门的事实句判定是规则驱动的。** 判不出类型时默认按事实句处理（要求绑定
  引用），这是刻意选的安全侧：漏出无据断言的代价高于多拦一句过渡语。代价是偶尔会
  误拦无害的连接句。Markdown 标题、加粗标签、分隔线、缺据声明句已豁免。
- **计算派生尚未强校验。** 派生句的结论数值本来就不在证据里，目前只要求整个回答里
  有可作为推导输入的有据事实句。`calc/` 就位后应改为由计算器产出并回填校验。
- **向量通道关闭。** 网关没有 embedding 模型，描述性查询依赖 `search_keywords` 的覆盖质量。
- **混合词缀在普通复制下仍会失配。** 带 ` / ` 的多行词缀（如「+X% 全抗 /
  召唤生物 +Y% 抗性」）在库里是一条、在装备上是两行，而普通复制看不出这两行属于
  同一条词缀。进阶复制能看出来（同一个 `{ }` 头下），此时会正确拼接后反查。
- **技能石 / 万神殿 / Boss 尚未入库。**（底材与天赋树已入库。）PoB 有完整数据但无中文名，需先补 POEDB 对应中文页，
  否则 `name_zh` 只能填英文，违背中文输出要求。这条限制会传染到装备分析：隐式词缀
  查不到、护甲类只能按整个类别（多个底材页面）查 T 级而定不到具体页面。
- **稀有装备的前后缀数量上限没有可引用来源。** `game_constant` 里查无此键，所以
  装备分析只报「已用前缀 N 条、后缀 M 条」这个事实，不断言「还能加几条」。
- **PoB 唯一装备的历史变体已过滤但未入库。** 2157 条被排除的历史词缀目前丢弃了。
  存下来能回答版本改动类问题，但必须与当前数据严格隔离，否则会被当成现值输出。
- ~~POEDB 侧 76% 的唯一装备缺需求等级。~~ **已解决**（2026-08-18 复核）：
  `unique_l2` 的详情页补全把这个字段填满了，现在 1342 件**全部**有需求等级
  （范围 1–84，中位数 49）。反倒是 **PoB 侧只有 324/1300 件带 `level_req`**，
  所以能双向交叉核对的就是这 324 件，其中 27 件不一致、已登记为 `unique_review` 待人工核对。
- **唯一装备的 `mod_count` 两源只有 52.9% 一致。** 这是建模差异不是数值分歧：
  PoB 把 `Elder Item` 这类影响力标记也算独立词缀，POEDB 不抽取。已降级为结构指标。

---

## 授权与致谢

游戏数据版权归 Grinding Gear Games。
Path of Building Community 以 MIT 授权，其 `src/Data/` 内的游戏数据同样版权归 GGG。
本项目仅在本地使用这些数据，并在输出中链回原始站点。
