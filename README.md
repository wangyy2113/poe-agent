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
| 知识库 | POEDB 唯一装备 1342 件、词缀 94406 条（含 T 级 / 物品等级 / 生成权重）、别名 8382 条 |
| PoB 数据源 | 词缀 25487 条、唯一装备 1300 件、游戏常量 543 条；引用钉住 commit |
| 技能石与辅助宝石 | 820 颗（558 主动 + 262 辅助）+ 30672 行每级字面值 + **每级效果文本与属性需求**（POEDB 详情页逐级表，812 颗逐级对过账）；PoB 出结构、POEDB 出中文名，按 `grantedEffectId` 桥接，清单页没收录的再按详情页标题兜底（未桥接只剩 1 颗）。**每级基础伤害已入库**（175 颗技能 7000 行，按 PoB 的 effectiveness 公式算出显示值，与 POEDB 技能页对账 20/20）；固定效果、获取途径及技能与辅助宝石的双向确认关系也已入库。**DPS 仍不算**（缺整条加成链） |
| 通货 | 113 条（中英文名、堆叠上限、效果原文）；工艺台费用引用的 18 个通货全部能对上；**获取途径与兑换比例未采集**；市场价格走 poe.watch 实时快照 |
| 圣甲虫 | 118 个（效果、堆叠上限、地图装置放置上限）；**掉落来源未采集**；市场价格走 poe.watch 实时快照 |
| 实时经济价格与材料清单成本 | `POST /economy/price` + `economy_price_lookup`：poe.watch 当前/指定联盟的唯一装备、通货、圣甲虫、命运卡、碎片价格；`POST /economy/material-cost` + `economy_material_cost`：对用户明确给出的材料与数量逐项计算 mean/min/max 小计和总计。SQLite 30 分钟 TTL，刷新失败降级旧快照，超过 6 小时显式标陈旧；每项输出来源、联盟、抓取时间与低置信度标记。**不生成制作材料或路径，不含稀有装备估值、成功率、趋势、利润与 Flip 建议** |
| Boss 技能伤害 | 10 条 Boss 技能的伤害建模 + 22 个 Boss 的护甲/闪避倍率；`boss_skill_damage` 按 `monsterDamageTable` 与 PoB 系数算出某等级的一击伤害（谢勒的球 84 级 9,588–14,382 冰霜），未指定掷骰区间时给两端。**只给伤害建模，不给打法**——技能机制、阶段与应对未采集；数字是 PoB 建模值，GGG 未公布 |
| 制作规则（名额） | 7 条前后缀名额规则（魔法 1+1、稀有 3+3、传奇不受限制、药剂随魔法，另有 PoB 独有的珠宝/Graft 2+2 与「已腐化深渊珠宝回到 3+3」）；`check_mod_combination` 因此**第一次能返回 `possible`**，超名额则 `impossible`。`possible` 的含义是「这些词缀能共存」而非「能做出来」；**影响力条件与元词缀状态机仍未采集** |
| 收获（古灵庄园） | 74 条定向工艺 + 98 条命能费用，且 **74/74 条均有结构化语义**：20 条装备重铸、23 条元素转换与新增/移除、14 条品质附魔、14 条材料随机转换、3 条地图/技能石/命运卡牺牲；另有 **51 个种子怪物**按颜色对应四种命能中的三种。随机类型与概率、堆叠输出数量、费用缩放基准和市场成本未采集时保持未知 |
| 精华 | 精华**赋予的词缀** 5985 条（在 `mod_affix`，已渲染进词缀组卡）+ **精华物品本身** 105 件（等阶 / 堆叠上限 / 等级上限 / 基础效果 / 632 行逐装备类别的保底词缀），与 PoB `Data/Essence.lua` 逐项对账（条数 105/105、装备类别数两侧均 22、tier 104/105，唯一分歧登记为 `essence_review`）；`essence_operation_state` / `POST /craft/essence` 按原文判断普通升级、稀有重置、物等上限与类别保底词缀；**掉落来源与兑换成本未采集** |
| 万神殿 | 12 位神祇、32 层神力、41 条词缀（中文词缀、抓获的头目与地图、给予神力的章节与任务；PoB × POEDB 两侧 `(stat_id, value)` 逐条对账全对）；**启用规则与神之容器的机制说明未采集** |
| 地图 | 159 张（等阶、头目、额外目标、命运卡、工艺台配方、图鉴相邻地图）+ 361 条地图词缀；**内部布局未采集** |
| 星团珠宝 | 3 个尺寸（小 2–3 / 中 4–6 / 大 8–12 个小天赋）+ 55 条小天赋类型（仅英文）+ 308 条核心天赋与基石清单；**哪种类型能开出哪些核心天赋未采集** |
| 天赋树路径 | 连线 3382 条 + 7 个职业起点入库；`passive_path` 按职业算「点到某节点要几点」并给出逐节点路径（总点数 123） |
| 怪物数据 | 怪物词缀 183 条（有中文文本的）+ 可作幽魂怪物 1948 个与其伤害% / 生命%，其中 268 个另有 PoB 的四抗性与建模系数；倍率排名在这 268 个里做；**Boss 技能与打法、掉落、怪物技能说明未采集** |
| 跨来源冲突 | 幽魂对账给出 8 条已确认分歧 → 答案里并列双方并推荐 POEDB（需求 15.5 首次可验收）；43 条粒度差异单列，不当冲突呈现 |
| 元词缀矩阵 | 页面矩阵 134 条（operation × guard，5 个小节）；空白格只进审计、不成为证据 |
| 基础通货状态机 | `craft_operation_state` / `POST /craft/operation`：按 POEDB 效果原文模拟增幅、崇高、重铸、神圣、剥离、改造、混沌、富豪、机会、点金 10 种基础通货；检查稀有度与前后缀名额，输出三态结论和条件后状态；特殊状态与来源未说明字段保持未知，**不生成结果词缀、成功率、步骤、成本或路线** |
| 古灵通货状态机 | `eldritch_operation_state` / `POST /craft/eldritch`：逐字覆盖四档余烬、四档溶液、古灵混沌/崇高/剥离石与冲突石共 12 种；检查装备部位、首要影响、前后缀名额和双基底词缀状态，并列披露已有元词缀矩阵原值。随机结果、概率、未给出的档级推进与特殊状态保持未知 |
| 隐匿通货状态机 | `veiled_operation_state` / `POST /craft/veiled`：逐字覆盖加密崇高石与加密混沌石；检查稀有度、可移除词缀和特殊状态，并按底材类别接通 1,299 条隐匿来源词缀记录（36 个中文类别、80 个词缀组）。候选目录不是单次揭露选项，页面权重不作为结果概率；爱斯林操作效果未采集 |
| 化石状态机 | `fossil_operation_state` / `POST /craft/fossil`：精确覆盖 18 件化石、37 条标签乘数，并按底材类别与可选物等汇总 1,983 条地渊来源词缀。`x0` 明确标为屏蔽，其余乘数逐标签披露；不合并多标签乘数，不把目录或基础权重冒充最终概率。共鸣器组合、完整执行契约与稀有度变化保持未知 |
| 跨来源对账 | 词缀区间一致 86.4%、唯一装备实质字段一致 83.5%，差异登记待核对 |
| 游戏常量 | `calc/constants.py`，逐个常量可溯源到 GGG 原始表；**缺失即抛异常，不给默认值** |
| 受击结算 | `calc/mitigation.py` + `mitigation_calc` 工具：闪避与命中率（怪物命中值取自库里的等级表）、格挡几率与上限、法术压制（减 **40%** 不是 50%）、护甲对某一击的减伤（**护甲不是固定百分比减伤**，不给伤害数值就没有答案）。**不含头目的护甲/闪避倍率** |
| 暴击 | `calc/crit.py` + `crit_calc` 工具：暴击率（基础 + 定值）×(1 + 增加)× 更多、夹在 **100%** 上限内（不是 95%），以及平均伤害倍率 =(1 − 暴击率)+ 暴击率 × 暴击加成。基础暴击按技能名或武器底材名查库。**只给倍率不给 DPS**（伤害数值未采集） |
| 命中与攻速 | `calc/accuracy.py` + `accuracy_calc`：命中率 = 取整(命中值 /(命中值 +(敌方闪避 / 5)^0.9)× 125)，夹在 **5%–100%**，再乘敌方格挡；敌方闪避按等级查表（83 级普通怪 7,860），也能**反解**「要多少命中值才有 90% 命中率」（1,899）。`calc/attack_speed.py` + `attack_speed_calc`：武器基础攻速 ×(1 + 技能倍率)，再除以 round((1 + 加成%)× 更多, 2)。**都不给 DPS**，也不含头目闪避倍率与双持 |
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
| **OpenAI 兼容** | `POST /v1/chat/completions`：支持文本消息、非流式与标准 SSE `[DONE]`；内部仍强制检索与出口校验 |
| **MCP stdio** | `mcp_server.py`：按 MCP `2025-06-18` 向 Codex、Claude Desktop 等客户端暴露 Knowledge Agent 的 44 个只读工具；支持 `--tools` 白名单，工具结果保留结构化数据、引用、unknown/boundary 与耗时 |
| **鉴权与限流** | 普通接口使用 `API_KEYS` 并按 Key 令牌桶限流；管理接口独立使用 `ADMIN_TOKEN`，可叠加 IP/CIDR 白名单 |
| **出口闸门** | 逐句校验事实断言与计算派生是否绑定有效证据，未通过由代码拦截；47 项单测覆盖 |
| **聊天界面** | `static/` 单页前端，无构建步骤、无 Node、无第三方 JS |
| **会话历史** | 最近 30 个会话保存在浏览器列表中，可从 SQLite 回放消息并重命名；自定义标题不会被后续消息覆盖 |
| **装备解析** | 游戏内 Ctrl+C / Ctrl+Alt+C 文本 → 结构化装备；中英文都认 |
| **词缀 T 级** | 按底材类别 + 数值区间定档；查不到就标「未匹配」并说明原因 |
| **装备分析** | `POST /item`：基础信息、词缀档位、前后缀容量、可制作方向，全程不调 LLM |
| **Build 体检** | PoB 分享码、白名单托管链接、官方天赋树、GGG OAuth 角色 API 或截图 → 核心体检、防御来源、输出分析与双档建议；截图先经视觉模型转录并由用户确认；只使用各输入实际携带的字段，缺失项保持「未知」 |
| LLM 客户端 | 直连 LiteLLM 网关，多 Key 故障转移、分级冷却、推理模型适配 |
| 缺口台账 | `knowledge_gap` 按频次排序即采集优先级；**评测流量与永久越界（POE2）不计入**（否则前两名会是本该拒答的黄金用例），`/admin/stats` 同时列出被排除的条目数 |
| 执行轨迹 | 每轮落库，含拦截原因与 Token 明细，`/admin/traces` 可追溯 |

**还没实现**

- 稀有装备逐词缀市场估值（所以装备分析里「值不值得裂界」仍输出「数据不足」；有稳定名称的条目价格已可查）
- **DPS**：命中率、攻速、暴击倍率、基础伤害都有了，但整条加成链（增加的伤害 / 更多的伤害 / 伤害转化 / 抗性穿透）没有采集，所以仍然拒答。伤害转化下的 Max Hit、冰冻时长曲线同样还没有

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
#    POE_OAUTH_TOKEN=<可选；角色导入需要 account:characters scope>
#    POE_OAUTH_USER_AGENT=<OAuth clientId/version (contact: 联系方式)>

# 3) 自检：确认运行环境就绪，并检查跨平台代码约束
.venv/bin/python scripts/selfcheck.py
.venv/bin/python scripts/check_portability.py

# 4) 建知识库（首次会抓 POEDB，之后走本地缓存）
.venv/bin/python -m kb.pipeline --category all
#    可单独跑：unique_item / unique_l2 / mods / bench（工艺台目录）
#              / horticraft（收获定向工艺）/ metamods（元词缀矩阵）
#              / tree（天赋树与 Atlas）/ coverage（只刷覆盖声明）

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

### 作为 MCP 工具服务器使用

`mcp_server.py` 是本地 stdio 服务，不需要先启动 Web 服务，也不监听端口。默认暴露
Knowledge Agent 的 44 个只读工具；需要缩小能力面时传入逗号分隔的白名单：

```bash
.venv/bin/python mcp_server.py --tools kb_coverage,entity_lookup
```

Codex CLI 可直接注册（把两处路径替换为本机绝对路径）：

```bash
codex mcp add poe-agent -- \
  /绝对路径/poe-agent/.venv/bin/python \
  /绝对路径/poe-agent/mcp_server.py
codex mcp list
```

也可以写入 `~/.codex/config.toml`；Codex CLI、IDE 扩展与桌面端共用这份配置：

```toml
[mcp_servers.poe-agent]
command = "/绝对路径/poe-agent/.venv/bin/python"
args = ["/绝对路径/poe-agent/mcp_server.py"]
cwd = "/绝对路径/poe-agent"
```

Claude Desktop 的 `claude_desktop_config.json` 使用同一个入口：

```json
{
  "mcpServers": {
    "poe-agent": {
      "command": "/绝对路径/poe-agent/.venv/bin/python",
      "args": ["/绝对路径/poe-agent/mcp_server.py"],
      "cwd": "/绝对路径/poe-agent"
    }
  }
}
```

重启客户端后应能看到 `kb_coverage`、`entity_lookup` 等工具。服务进程的 stdout 严格只写
MCP JSON-RPC；诊断信息写 stderr。客户端必须保留工具返回的 `unknown`、`boundary` 与
`citations`，不得用模型记忆补全机制、概率、价格或制作路线。Codex 配置字段可对照
[官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

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

**把装备文本、PoB Build 或官方天赋树链接贴进输入框**会触发识别（需求 6.3/6.4）：识别后先弹一张
卡片让你确认，不直接分析。两个理由——识别可能判错，判错了直接分析等于你莫名收到
一份不相干的报告；而且你粘贴的可能只是问题的一部分（贴完装备还想补一句「值多少钱」），
直接触发就把下文吞了。PoB 分享码、白名单托管链接和 `pathofexile.com` 官方天赋树链接
会进入确定性的 Build 体检，不调用 LLM；其中的数值取自输入本身，本库只做判读。
顶部“导入角色”按钮可按角色名读取当前 GGG OAuth 账号中的 PoE1 PC 角色。

左上角可切换历史会话（列表存 `localStorage`，消息与标题存 SQLite），也可重命名当前
会话；点「新对话」重开。

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

对外提供服务时应在 `.env` 配置逗号分隔的 `API_KEYS`，并在普通业务请求中发送
`Authorization: Bearer <key>`。`config.yaml` 的 `server.rate_limit` 默认允许每个 Key
持续 60 次/分钟、最多突发 10 次；超限返回 HTTP 429 和 `Retry-After`。未配置
`API_KEYS` 的本机开发模式无需鉴权，但所有请求共用一个 `anonymous` 限流桶。

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

**OpenAI Chat Completions 兼容接口** —— `POST /v1/chat/completions`。适用于 OpenAI SDK
和只认标准 Chat Completions 协议的客户端；实际路径仍服从 `server.base_path`，默认是
`/poe_agent/v1/chat/completions`。

```bash
curl -s http://127.0.0.1:8787/poe_agent/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"poe-agent","messages":[{"role":"user","content":"猎首有什么词缀？"}]}'
```

设置 `"stream": true` 后返回标准 `chat.completion.chunk` SSE，并以 `data: [DONE]`
结束；`stream_options.include_usage=true` 时在结束前增加 usage 帧。兼容层只使用最后一条
非空 `user` 文本，外部 `system` 消息不会覆盖本 Agent 的检索和安全约束。请求里的
`model` 是客户端逻辑名称；实际网关模型、`trace_id`、会话 ID、缺据状态、拦截数和结构化
引用放在 `poe_agent` 扩展字段中。若要延续服务端会话记录，可额外传 `conversation_id`。

**实时价格** —— `POST /economy/price`，按精确名称查询当前或指定联盟；中文唯一装备、
通货与圣甲虫名会先经本地权威别名解析。响应中的 `mean_chaos` / `min_chaos` /
`max_chaos` 是 poe.watch 统计快照，不是保证成交价。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/economy/price \
  -H 'Content-Type: application/json' \
  -d '{"query":"猎首","category":"auto"}'
```

响应始终带 `league`、`source`、`fetched_at`、`from_cache`、`stale`、`degraded` 和
`warning`；上游失败且完全没有缓存时返回 **503**，不会猜一个价格。

**明确材料清单成本** —— `POST /economy/material-cost`。每项都必须给名称和正数数量；
服务逐项精确匹配，并用同一联盟的 `mean/min/max × quantity` 做确定性加总。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/economy/material-cost \
  -H 'Content-Type: application/json' \
  -d '{"materials":[{"query":"神圣石","quantity":2},{"query":"混沌石","quantity":100}]}'
```

若任一材料未找到、同名结果不唯一或缺少均价，`calculable=false` 且总均价为空，绝不会把
已查到的部分冒充完整总成本。它只计算输入清单，不会反推某件装备需要哪些材料、成功率或工艺步骤。

**基础通货状态转移** —— `POST /craft/operation`。支持 10 种已审计基础通货，输入可为
稀有度与前后缀计数，也可直接给完整装备复制文本；两种状态输入不能混用。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/craft/operation \
  -H 'Content-Type: application/json' \
  -d '{"operation":"崇高石","rarity":"稀有","prefix_count":2,"suffix_count":3}'
```

该例返回 `compatible`，条件后状态只能是 3 前缀 + 3 后缀。这里的兼容只证明 POEDB
效果原文中的稀有度前提与结构化名额规则通过，不代表完整游戏可执行性；具体新增词缀、
数值、概率、步骤、成本与路线仍保持未知。腐化、镜像、裂隙等特殊状态不会被擅自外推。

**精华状态转移** —— `POST /craft/essence`。逐字使用精华页的基础效果、等级上限和
逐装备类别保底词缀，不按精华等阶推断规则。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/craft/essence \
  -H 'Content-Type: application/json' \
  -d '{"essence":"憎恨之破空精华","rarity":"稀有","item_level":84,"item_class":"项链"}'
```

该例返回 `compatible`、操作模式 `reroll_rare_properties`，并给出项链保底词缀
`冰霜伤害提高 (31—34)%`。低阶精华的物等上限、普通/稀有前提和真实 tier 0 例外均按
页面字段判断；额外随机词缀、最终数值、成功率、步骤、成本与路线仍保持未知。

**Harvest 重铸状态转移** —— `POST /craft/harvest/reforge`。支持 20 条已审计装备重铸；
可用完整效果原文，也可用 `火焰`、`影响词缀`、`大概率相同`、`随机影响类型` 等确定性别名。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/craft/harvest/reforge \
  -H 'Content-Type: application/json' \
  -d '{"operation":"火焰","rarity":"稀有"}'
```

响应保留页面原文“其中包括一个火焰词缀”和原始命能费用，但不把保底标签改写成具体词缀，
也不把“大概率”换算成数值概率。元素词缀转换、新增/移除、品质附魔与材料转换/牺牲
分别由下列独立端点处理，不会被猜成装备重铸。

**Harvest 定向词缀状态转移** —— `POST /craft/harvest/modifier`。支持 12 条元素伤害/抗性
近似位阶转换，以及 11 条新增指定标签并从不受影响物品移除另一个随机词缀。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/craft/harvest/modifier \
  -H 'Content-Type: application/json' \
  -d '{"operation":"火焰抗性转冰霜抗性","source_modifier_present":true}'
```

该例返回 `compatible`、火焰抗性 → 冰霜抗性的条件变化和原始费用。输出只保留
“近似位阶”，不声称相同位阶；新增/移除类也只给标签与随机移除语义，不猜具体词缀。

**Harvest 品质附魔状态转移** —— `POST /craft/harvest/quality-enchant`。支持 8 条护甲
与 6 条武器附魔；会区分护甲、武器和近战武器，并替换品质的默认防御/物理伤害收益。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/craft/harvest/quality-enchant \
  -H 'Content-Type: application/json' \
  -d '{"operation":"护甲火焰抗性","item_kind":"body_armour","quality":20}'
```

该例按原文确定为品质不再增加防御、20% 品质提供 10% 火焰抗性。品质不能整除原文
步长时保持 `unknown`，不擅自补取整方式；已有附魔、特殊状态与市场成本同样不外推。

**Harvest 材料转换与牺牲** —— `POST /craft/harvest/exchange`。覆盖剩余 17 条：
14 条辅助宝石/材料/碎片/精华/命运卡转换，以及腐化稀有地图、腐化技能石经验、
半叠命运卡三种牺牲。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/craft/harvest/exchange \
  -H 'Content-Type: application/json' \
  -d '{"operation":"命运卡赌博","quantity":5,"max_stack_size":10}'
```

该例只确定输出为 0–10 张相同命运卡，不生成每个结果的概率。材料转换保留随机、同色、
同位阶、同类别与“化石数量可能变少”等原文；页面费用与按堆叠比例缩放分开披露。

**古灵通货状态转移** —— `POST /craft/eldritch`。覆盖 12 种古灵通货，并区分
装备部位、焚界者/灭界者哪一方首要、目标侧前后缀名额与双基底词缀状态。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/craft/eldritch \
  -H 'Content-Type: application/json' \
  -d '{"operation":"古灵崇高石","rarity":"稀有","item_slot":"body_armour","dominance":"焚界者","prefix_count":2,"suffix_count":3}'
```

该例只确定添加一个随机前缀并把前缀计数变为 3；不会生成具体词缀或概率。
冲突石只保留“随机升一降一、被降低的次级词缀移除”的原文边界，不推断确切档级推进。

**加密通货状态转移** —— `POST /craft/veiled`。覆盖加密崇高石与加密混沌石；
指定底材类别时，同时返回本库 `category='veiled'` 的去重候选目录与前后缀分布。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/craft/veiled \
  -H 'Content-Type: application/json' \
  -d '{"operation":"加密崇高石","rarity":"稀有","item_class":"胸甲","prefix_count":2,"suffix_count":2}'
```

该例只确定随机移除一个词缀并添加一个随机加密词缀，同时枚举可能的前后缀计数。
候选目录不等于一次揭露实际展示的选项，页面生成权重也不会被换算成操作结果概率。

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

前后缀容量
- 当前 1 前缀 / 1 后缀；稀有装备上限 3 / 3[[E2]]
- 可用随机词缀位：前缀 2 条、后缀 2 条[[E2]]

可制作方向
- `+107 最大生命`
  - 该底材上最高档位是 T1（115-129），要求物品等级 54[[E1]]
  - 互斥：+(41-45) 最大生命 / 每秒再生 4 魔力（工艺工作台 T1/6）

市场价值
**无价格数据。** poe.watch 可查标准条目，但尚不能按稀有装备逐词缀估值……**不按同类物品类推价格**
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
- **不给稀有装备估值。** poe.watch 实时源已经接入，但它不能按一件稀有装备的逐词缀组合估值；所以「是否值得裂界 / 镜像湖 / 够不够镜子级别」
  三项一律输出「数据不足，不作判断」+ 缺哪项数据，同时把已知事实（档位、roll
  百分比、腐化状态）摆出来交给你判断。给一条「建议裂界」会让人真去花分裂石，
  而我们连该词缀裂界后值多少都查不到。

格式认不出时返回 **422**，带失败原因与期望格式样例，不返回半个解析结果：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8787/poe_agent/item \
  -H 'Content-Type: application/json' -d '{"text": "今天天气不错"}'
# 422
```

**Build 体检** —— `POST /build`，接受分享码、白名单托管链接、官方天赋树链接，
或当前 GGG OAuth 账号中的 PoE1 PC 角色名。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/build \
  -H 'Content-Type: application/json' \
  -d '{"code": "https://pobb.in/你的分享ID"}'

# 官方天赋树链接同样走此端点，全程本地解码，不请求该 URL
curl -s -X POST http://127.0.0.1:8787/poe_agent/build \
  -H 'Content-Type: application/json' \
  -d '{"code": "https://www.pathofexile.com/passive-skill-tree/3.29.0/你的编码"}'

# 角色导入：先在 .env 配置 POE_OAUTH_TOKEN 与 GGG 规定格式的 POE_OAUTH_USER_AGENT
curl -s http://127.0.0.1:8787/poe_agent/build/characters
curl -s -X POST http://127.0.0.1:8787/poe_agent/build \
  -H 'Content-Type: application/json' \
  -d '{"character_name": "你的角色名"}'
```

返回输入中可取得的职业、技能组、装备与天赋；PoB 输入还会返回其自带的 `PlayerStat`。
所有输入都进入需求 8.4 的 16 项体检、需求 8.5 的防御补充（最大抗性、吸纳、偷取、
回复）和需求 8.6 的三类输出分析。
单体栏只复述 Calcs 当前技能的输出快照；清图栏只展示范围、投射物、穿透、分裂、
连锁与弹跳等确实存在的覆盖字段；Boss 栏只有在 `enemyIsBoss` 明确保存为新版
`Boss/Pinnacle/Uber` 或旧版 `Shaper/Sirus` 时才给值，不把普通怪快照冒充 Boss 输出。
报告还会列出装备文本里可逐字核对的防御来源；天赋、技能、配置、升华与药剂没有
PoB breakdown 时明确标成尚未拆分，不把它们当作 0。对已知且低于明确上限的元素抗性、
混沌抗性与法术压制，报告给出低/高预算两档建议和可复核的目标差额。标准条目价格与
**用户明确提供的材料清单**成本已接入，但系统仍不会自行生成完整制作路径或材料清单；
Build 建议中的成本只按改造规模分档，不虚构 chaos/divine 方案报价。
当前激活天赋方案的 `treeVersion` 还会与本地 `passive_graph_node`（缺失时退回
`game_constant`）版本比对；不一致时提示宝石桥接、规则判读和建议可能存在偏差，
但不会否定原 PoB 已保存的 PlayerStat 快照。
PoB、天赋树、角色 API 与截图确认后的体检阶段不调用 LLM（只有截图转录阶段调用视觉模型）。
体检是“只判不算”：数值来自用户提供并确认的 Build 输入，本库只用
有出处的规则判读；缺项明确标为“未知”，不会尝试部分重算或估计。
天赋树链接只携带职业编号、升华/Bloodline 编号、节点与大师选择，不含装备、技能组、
配置或 PlayerStat；因此这些缺失维度会显示“未知”，不会由树节点反推一份虚构的完整 Build。
角色 API 使用 GGG 当前官方 `GET /character/<name>`，token 只保存在服务端且不落缓存；
导入已装备物品、插槽连接组、珠宝、天赋、盗贼、万神殿与版本。背包不属于已装备 Build，
不会混入体检；官方响应不含 PoB PlayerStat 和主技能选择，这些维度同样保持“未知”。

**截图识别** —— 聊天页点击“上传截图”，支持不超过 5 MB 的 PNG、JPEG 与 WebP。
先在 `config.yaml` 的 `llm.vision_model` 填入网关中实际支持 OpenAI `image_url` 消息的
视觉模型；留空时 `POST /screenshot/recognize` 明确返回 503。图片只在该次请求内转给
视觉模型，不写磁盘、不进数据库。模型只负责转录：装备截图会显示可编辑原文，确认后
进入 `/item`；Build 截图会列出白名单内的可见字段，确认后进入 `/build/screenshot`。
没有出现在截图中的药剂、诅咒、珠宝、天赋和 PlayerStat 一律标为“未知”，不会因为
画面没拍到就判成“没有”，也不会从职业或技能补全数值。当前没有已配置视觉模型的
真实截图基准，因此本轮只以模拟视觉响应验证契约，实际识别质量取决于所选模型。

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
vector = extension_available true, requested false, enabled false, reason="配置关闭"
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

需要再限制管理端来源时，在 `config.yaml` 的 `server.admin_allowed_ips` 填写 IP 或 CIDR，
例如 `["127.0.0.1", "::1", "10.0.0.0/8"]`；默认空列表表示只校验管理令牌。白名单只读取
ASGI 服务器看到的 TCP peer，刻意不信任 `X-Forwarded-For`。如果前面有反向代理，应把
代理 peer 加入白名单，并在代理层另外限制 `/admin/*`，不要把用户可写的转发头当来源证明。

注意 `/admin/conflicts` 里 `kind` 的区别：`mod` 是**已确认冲突**，会按需求 15.5
并列展示给用户；`mod_review` 只是「两源对不上，待人工核对」，不得当作冲突呈现。

### 检索结果里的 `degraded=true` 是什么意思

配置主动关闭时不是故障。2026-08-04 实测网关未注册任何 embedding / rerank 模型，所以
`config.yaml` 里 `retrieval.vector.enabled` 与 `rerank.enabled` 都是 `false`，
检索走 BM25 + 实体精确 + 词缀意图三路，`degraded` 标记的是「向量通道未参与」。
`/healthz` 会分开报告配置请求值 `requested` 与运行时有效值 `enabled`。即使配置误设为
`true`，扩展不可加载、`chunk_vec` 未建立或查询适配器尚未接入时也会自动按关闭处理，
在启动日志、健康检查和检索附注中写明原因，不会把一个未执行的 `vector` 通道列进元数据；
BM25 与实体精确检索继续可用。

---

## 数据源与优先级

优先级内建于检索排序，不是提示词里的建议。

| 级别 | 来源 | 说明 |
| --- | --- | --- |
| P1 | POEDB | 中文名与页面说明的真源，最高优先级；冲突时推荐采用 |
| P2 | Path of Building `src/Data/` | 数值与结构的真源（GGG 客户端 `.dat` 机器导出），无中文名 |
| P3 | PoE Wiki | 尚未接入 |
| P4 | Craft of Exile | 尚未接入 |
| P5 | poe.watch | 已接入实时价格快照；统计值不是保证成交价 |
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
| `scripts/check_portability.py` | AST 平台中立性检查：平台专有模块、shell 依赖、路径 API 与文本编码 |

| `scripts/eval_run.py` | 黄金问答集评测：检索侧指标 / 录制回放 / 出口闸门指标 / 人工审计清单 |
| `scripts/import_pob_bases.py` | 导入 PoB 底材注册表，18 个装备类别（默认不联网；`--dry-run` 清点、`--category` 单类、`--fetch` 缺页补抓） |

### 评测：四项指标与黄金问答集

`config.yaml` 里的阈值与 `orchestrator/binding.py` 的闸门规则都需要判据，否则改动之后
无法判断是变好还是变坏。判据来自 `tests/golden/questions.yaml`（158 条，六类样本：
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

目前 1717 项，其中与「无据不答」直接相关的：

| 组 | 项数 | 重点 |
| --- | --- | --- |
| 出口绑定闸门 | 47 | 事实数值溯源、计算器派生回填与成对的错误拒答/无据漏出压制 |
| 装备文本解析 | 45 | 真实 Ctrl+C 文本 fixture；「普通复制不许带 T 级」「认不出必须显式失败」 |
| 词缀 T 级匹配 | 26 | 内存桩库，数值照抄真实库；「匹配不上必须标未匹配」 |
| 装备分析报告 | 20 | 「不给稀有装备估值」「容量输入不完整不报余量」「最高档位不跨来源类别」 |
| 实时经济价格与材料成本 | 16 | 精确名称、批量清单加总、计价单位、缺价不报部分总价、联盟、TTL、降级、陈旧与低置信度披露 |
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
                          chat.py / search.py / economy.py / craft.py / item.py / admin.py / deps.py（鉴权）
economy/                poe.watch 适配、领域模型、SQLite 快照与降级策略
orchestrator/           主循环、出口绑定闸门、引用装配
                          loop.py / binding.py / citation.py
agents/                 领域 Agent：提示词 + 允许工具集 + 输出契约
tools/                  工具注册与实现。顺序即推荐优先级：
                          economy_price_lookup / economy_material_cost / affix_pool_odds / check_mod_combination
                          / craft_operation_state / essence_operation_state / eldritch_operation_state
                          / veiled_operation_state / fossil_operation_state / harvest_reforge_state
                          / harvest_modifier_state
                          / bench_recipe_catalog
                          / metamod_interaction_matrix / mod_tiers / skill_gem_levels
                          / spectre_stats / passive_path / cluster_jewel_info
                          / resistance_calc / mitigation_calc / recovery_calc / reservation_calc
                          / ailment_dot_calc
                          / ailment_threshold_calc
                          / max_hit_calc / crit_calc / accuracy_calc / attack_speed_calc
                          / stat_sources
                          / entity_lookup / kb_coverage / kb_search（兜底）
craft/                  制作分析：feasibility.py 组合三态可行性 / pool.py 胸甲条件权重概率
                          / operations.py 基础通货保守状态转移
                          / essence_operations.py 精华原文约束与保底词缀状态转移
                          / fossil_operations.py 化石标签权重变换与地渊来源词缀目录
                          / harvest_operations.py 20 条 Harvest 装备重铸状态转移
                          / harvest_modifier_operations.py 23 条 Harvest 定向词缀状态转移
                          / harvest_enchant_operations.py 14 条 Harvest 品质附魔状态转移
                          / harvest_exchange_operations.py 17 条 Harvest 材料转换与牺牲状态转移
calc/                   常量访问层与计算器（constants.py 缺失即抛异常 / derive.py 推导件
                          / resistance.py 抗性
                          / mitigation.py 受击结算
                          / recovery.py 回复
                          / reservation.py 保留
                          / ailment.py 持续伤害
                          / threshold.py 门槛型异常
                          / maxhit.py 一发最大承受 / crit.py 暴击
                          / accuracy.py 命中率 / attack_speed.py 攻速）
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
- **PoB Build 体检只判不重算。** 分享码与白名单托管链接都能解析；核心 16 项、
  防御补充和单体 / 清图 / Boss 输出分析都只消费 Build 自带的 `PlayerStat` 与配置。
  旧版分享码没导出的字段会标「未知」；Boss 敌人档位未显式保存时也标「未知」；
  清图只列覆盖指标，不给强弱评级。DPS 是复述 PoB 的结果，不代表本库已经实现整条
  伤害加成链，也不会自动切换敌人配置重算。装备侧防御来源只列原文，不求和；
  双档建议只对有明确数值与上限的短板触发，成本是改造规模而非实时通货价。
- **Build 版本核对使用当前激活天赋方案。** 多套天赋树时按 `<Tree activeSpec>` 选择，
  不再固定读取第一套。若 Build 没有 `treeVersion` 或本地库没有版本标记，会明确显示
  “无法判断”，不会默认当成同版。
- **角色 API 需要用户自己的 GGG OAuth token。** 当前只支持 PoE1 PC realm，且不会
  自动刷新 token；`POE_OAUTH_USER_AGENT` 必须符合 GGG 的
  `OAuth clientId/version (contact: contact)` 格式。
- **截图识别需要单独指定视觉模型。** `config.yaml` 的 `llm.vision_model` 默认留空；
  未经真实模型验证前不替用户选择一个名字。视觉模型只转录可见内容，用户确认后的字段
  才进入确定性分析；图片不落盘，单张上限 5 MB。
- **会话列表只存在浏览器 `localStorage` 里**（上限 30 条）。换浏览器或清缓存就看不到
  列表了——消息与自定义标题本身在 `data/poe.db` 里，也可以重命名当前会话，但没有按
  用户维度检索会话的接口，所以服务端不会自动恢复浏览器已经遗失的列表。
- **`.env` 里的 `LLM_API_KEY` 需要填真值。** 模板里是占位符 `sk-`；不填的话
  有据路径会返回「生成失败」，但缺据路径仍能正常工作（它不调模型）。
- **出口闸门的事实句判定是规则驱动的。** 判不出类型时默认按事实句处理（要求绑定
  引用），这是刻意选的安全侧：漏出无据断言的代价高于多拦一句过渡语。代价是偶尔会
  误拦无害的连接句。Markdown 标题、加粗标签、分隔线、缺据声明句已豁免。
- **复合问题的缺口由代码兜底披露。** 同一问题同时包含已覆盖与未覆盖维度时，有据部分
  正常生成并过出口闸门；未覆盖部分由代码追加采集状态与权威查证入口，同时写入缺口台账，
  不依赖模型是否记得主动声明。
- **数值计算派生只接受本地计算器结果。** 带“因此/合计/换算”等标记的数值结论必须
  绑定确定性计算器输出，且逐值精确匹配；模型自行心算、换算或从证据区间任选一点都会
  被拦截。尚无对应计算器的领域因此不能给派生数值。
- **向量通道关闭。** 网关没有 embedding 模型，描述性查询依赖 `search_keywords` 的覆盖质量。
- **混合词缀在普通复制下仍会失配。** 带 ` / ` 的多行词缀（如「+X% 全抗 /
  召唤生物 +Y% 抗性」）在库里是一条、在装备上是两行，而普通复制看不出这两行属于
  同一条词缀。进阶复制能看出来（同一个 `{ }` 头下），此时会正确拼接后反查。
- **技能石 / 万神殿 / Boss 都是部分覆盖，不是未入库。** 技能石与万神殿已经有中文
  数据和结构化出口；Boss 当前只有 PoB 的伤害建模与倍率，没有机制说明和应对打法。
- **装备报告的剩余名额要求输入完整。** `/item` 与 `check_mod_combination` 已共用
  `craft_affix_capacity`；普通装备、药剂、珠宝、Graft 与已腐化深渊珠宝按对应规则
  判断。但存在未识别显式词缀、隐匿占位、未鉴定状态或未归类行时只报告已识别数量，
  不声称还剩几个位置。
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
