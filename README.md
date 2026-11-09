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
| 技能石与辅助宝石 | 820 颗（558 主动 + 262 辅助）+ 30672 行每级字面值 + **每级效果文本与属性需求**（POEDB 详情页逐级表，812 颗逐级对过账）；PoB 出结构、POEDB 出中文名，按 `grantedEffectId` 桥接，清单页没收录的再按详情页标题兜底（未桥接只剩 1 颗）。**每级基础伤害已入库**（175 颗技能 7000 行，按 PoB 的 effectiveness 公式算出显示值，与 POEDB 技能页对账 20/20）；固定效果、获取途径及技能与辅助宝石的双向确认关系也已入库。显式给齐伤害链参数时可计算命中 DPS；仍不会从整套装备自动汇总所有修正。 |
| 通货 | 113 条（中英文名、堆叠上限、效果原文）；工艺台费用引用的 18 个通货全部能对上；**获取途径与兑换比例未采集**；市场价格以 poe.watch 为主、GGG Trade API 在线挂单为备用 |
| 圣甲虫 | 118 个（效果、堆叠上限、地图装置放置上限）；**掉落来源未采集**；市场价格以 poe.watch 为主、GGG Trade API 在线挂单为备用 |
| 实时经济价格、趋势、成本与双边价差 | `POST /economy/price` 查询当前快照；`POST /economy/trend` 查询历史；`POST /economy/material-cost` 汇总明确清单；`POST /economy/rare-comparables` 查询稀有装可比挂单，可选逐件核对品质与连接组尺寸；`POST /economy/flip-analysis` 对标准 exchange 条目核验买卖两侧、整单深度、时效与显式费用后计算净利润。SQLite 价格缓存 TTL 30 分钟、历史保留 90 天；主源无缓存时标准条目与唯一装备可显式降级官方 Trade API。**不足两个趋势点不判趋势；单侧过滤后不足 5 条不报代表价；任一侧或费用不完整时利润与 Flip 判断保持 unknown；所有结果都不是成交或收益保证** |
| Boss 技能伤害、生存校验与防御门槛 | 网页“Boss 命中风险”入口支持显式防御输入、批量风险审计与单技能门槛；10 条 Boss 技能的伤害建模 + 22 个 Boss 的护甲/闪避倍率；`boss_survival_check` 校验同一命中连续 1–100 次，`boss_defence_threshold` 反解最低防御池、抗性/护甲门槛，`boss_sequence_check` 按用户明确顺序逐击累计；`boss_timed_sequence_*` 结算显式时间轴、固定恢复与门槛；`boss_probabilistic_sequence_*` 计算闪避/格挡/压制后的低/高伤害生存分布与风险分位；`boss_layered_sequence_*` 分别推进生命、能量护盾、MoM 式魔力分流、各池回复和护盾回充中断；`boss_recovery_sequence_*` 再结算命中前后瞬回、定时恢复、偷取实例限速与满池溢出；`boss_dot_sequence_*` 用边界解析连续流统一结算显式 DoT 暴露、命中和恢复；`boss_mitigation_sequence_*` 在分层路由前结算显式临时承伤乘数、Guard 容量与精确耗尽；`boss_ward_sequence_*` 再逐击推进 Ward 破裂、恢复和吸收；`boss_risk_audit` 批量排列已收录技能风险。`POST /build` 可直接消费 PoB 基础防御、分池回复及适用概率字段快照。**序列不是真实连招，概率不是保证，审计不是 Boss 难度排行，数值工具不生成打法**；阶段与应对可另查 Wiki 攻略原文；数字是 PoB 建模值，GGG 未公布 |
| Boss 攻略原文与 Build 对照 | `GET /boss/guides` 与 `boss_guide_lookup` 查询八篇白名单 PoE Wiki 攻略；`POST /boss/guides/{boss_key}/build-check` 只将 Recommendations 中完整匹配的 DPS、生命加护盾和三元素抗性原句与用户 PoB 快照并排。**这是 P3 可变 Wiki 原文；不自动断定当前版本、药剂配置或实战通关** |
| 官方版本公告 | `scripts/import_ggg_patch_notes.py` 逐篇核对 GGG 官方 Patch Notes 分区、staff 首帖、日期和标题，原文进入 FTS；`/patch-notes/timeline` 与 `patch_note_lookup` 提供逐行英文历史提及，`/patch-notes/build-impact` 对 PoB 英文部件做字面匹配。**字面提及不是已生效版本或实测 Build 数值变化** |
| Atlas 目标候选与图路线 | `POST /planner/atlas` 按刷图速度、通货收益或特定材料目标找 POEDB 效果原文节点；`POST /planner/atlas-route` 把 1–10 个已选实体连接到 root，复用共同路径并列出新增图节点。两个对应的只读工具可用于问答与 MCP。**节点数不是已核验的点数，逐目标贪心连接不等于全局最优；收益和解锁条件仍未知** |
| 制作规则（名额与影响力） | 7 条前后缀名额规则（魔法 1+1、稀有 3+3、传奇不受限制、药剂随魔法，另有 PoB 独有的珠宝/Graft 2+2 与「已腐化深渊珠宝回到 3+3」）；`base_influence_profile` 可查 1,022 条具体底材的六类传统影响 `influenceTags`，`check_mod_combination` 在给定具体底材、单一影响与可选物等时，会用 PoB 底材标签和 POEDB 词缀 `spawn_tags` 验证影响词缀适用性。`possible` 只表示词缀能共存，不表示能制作；古灵基底由独立状态机处理。**影响底材获得方式、多影响词缀池组合与完整元词缀状态机仍未采集** |
| 独立尝试分布与材料预算 | 网页“独立尝试预算”入口可填写底材、目标、尝试上限与每次材料清单，并显式选择是否查询市场价；`craft_attempt_plan` / `POST /craft/attempt-plan` 在完整、新鲜的胸甲 `normal` 同侧词缀池上计算几何分布的单次条件概率、首次成功期望、P50/P90/P95、上限内成功/耗尽概率、提前停止后的期望消耗与最坏材料预算。**只适用于每次完整重置、独立同分布且每次恰好一次同侧加权选择；不是任何真实通货/工艺的一次成功率，不推断步骤或完整路线；任一材料缺价或联盟/来源/时间口径不一致时不输出部分总成本** |
| 觉醒者之石状态机 | `awakener_orb_state` / `POST /craft/awakener`：接受供体与目标的两段装备复制文本，或显式物品类别与传统影响；逐字绑定 POEDB 效果，检查同类物品、双方唯一且不同的传统影响，输出供体销毁、目标重铸为稀有并兼有两种影响的条件后状态。**不声称各保留一条影响词缀，不生成重铸后的具体词缀、数量、数值、概率、成本或路线** |
| 收获（古灵庄园） | 74 条定向工艺 + 98 条命能费用，且 **74/74 条均有结构化语义**：20 条装备重铸、23 条元素转换与新增/移除、14 条品质附魔、14 条材料随机转换、3 条地图/技能石/命运卡牺牲；另有 **51 个种子怪物**按颜色对应四种命能中的三种。随机类型与概率、堆叠输出数量、费用缩放基准和市场成本未采集时保持未知 |
| 精华 | 精华**赋予的词缀** 5985 条（在 `mod_affix`，已渲染进词缀组卡）+ **精华物品本身** 105 件（等阶 / 堆叠上限 / 等级上限 / 基础效果 / 632 行逐装备类别的保底词缀），与 PoB `Data/Essence.lua` 逐项对账（条数 105/105、装备类别数两侧均 22、tier 104/105，唯一分歧登记为 `essence_review`）；`essence_operation_state` / `POST /craft/essence` 按原文判断普通升级、稀有重置、物等上限与类别保底词缀；**掉落来源与兑换成本未采集** |
| 万神殿 | 12 位神祇、32 层神力、41 条词缀（中文词缀、抓获的头目与地图、给予神力的章节与任务；PoB × POEDB 两侧 `(stat_id, value)` 逐条对账全对）；**启用规则与神之容器的机制说明未采集** |
| 地图 | 159 张（等阶、头目、额外目标、命运卡、工艺台配方、图鉴相邻地图）+ 361 条地图词缀；**内部布局未采集** |
| 星团珠宝 | 3 个尺寸（小 2–3 / 中 4–6 / 大 8–12 个小天赋）+ 55 条小天赋类型 + 308 条核心天赋与基石清单；`cluster_jewel_info` 将同 commit 的类型 tag 与核心天赋词缀正权重连接，当前覆盖 52 个类型、568 条类型—核心关系，并支持物等过滤与双向反查。询问天赋点消耗时直接给尺寸证据并说明逐节点点数未采集，不调用 LLM。基础生成权重不冒充单次概率；**掉落与价格未采集** |
| 天赋树路径 | 连线 3382 条 + 7 个职业起点入库；`passive_path` 按职业算「点到某节点要几点」并给出逐节点路径（总点数 123） |
| 怪物数据 | 怪物词缀 183 条（有中文文本的）+ 可作幽魂怪物 1796 个及其伤害% / 生命%（2026-10-30 POEDB 快照），其中 268 个另有 PoB 的四抗性与建模系数；倍率排名在这 268 个里做；**本数据源不含 Boss 打法、掉落或普通怪物技能说明**；Boss 攻略另见 Wiki 原文 |
| 跨来源冲突与待核对差异 | 网页“跨来源核对”入口提供概览、筛选、分页与两侧原值详情；`POST /conflicts/lookup` 默认只返回 8 条 active confirmed 幽魂分歧，`GET /conflicts/summary` 将 547 条台账拆成 8 条 confirmed 与 539 条 review，`GET /conflicts/{id}` 精确读取单条。所有 `*_review` 必须显式请求，只能称“待核对差异”，引用不能支撑游戏事实，也绝不进入冲突并列块；游标绑定完整过滤条件，不能跨查询复用 |
| 元词缀矩阵 | 页面矩阵 134 条（operation × guard，5 个小节）；空白格只进审计、不成为证据 |
| 基础通货状态机 | `craft_operation_state` / `POST /craft/operation`：按 POEDB 效果原文模拟增幅、崇高、重铸、神圣、剥离、改造、混沌、富豪、机会、点金 10 种基础通货；检查稀有度与前后缀名额，输出三态结论和条件后状态。可选 `metamod_guard` 会把对应 Craft of Exile 矩阵原值作为独立证据并列披露；空白/缺失仍为 unknown，**不解释 YES/NO，不生成结果词缀、成功率、步骤、成本或路线** |
| 古灵通货状态机 | `eldritch_operation_state` / `POST /craft/eldritch`：逐字覆盖四档余烬、四档溶液、古灵混沌/崇高/剥离石与冲突石共 12 种；检查装备部位、首要影响、前后缀名额和双基底词缀状态，并列披露已有元词缀矩阵原值。随机结果、概率、未给出的档级推进与特殊状态保持未知 |
| 隐匿通货状态机 | `veiled_operation_state` / `POST /craft/veiled`：逐字覆盖加密崇高石与加密混沌石；检查稀有度、可移除词缀和特殊状态，并按底材类别接通 1,299 条隐匿来源词缀记录（36 个中文类别、80 个词缀组）。`GET /craft/veiled/operations` 与网页入口展示当前入库操作、条件后状态及完整分页目录。候选目录不是单次揭露选项，页面权重不作为结果概率；爱斯林操作效果未采集 |
| 化石状态机 | `fossil_operation_state` / `POST /craft/fossil`：精确覆盖 18 件化石、37 条标签乘数，并按底材类别与可选物等汇总 1,983 条地渊来源词缀。`x0` 明确标为屏蔽，其余乘数逐标签披露；不合并多标签乘数，不把目录或基础权重冒充最终概率。共鸣器组合、完整执行契约与稀有度变化保持未知 |
| 跨来源对账 | 词缀区间一致 86.4%、唯一装备实质字段一致 83.5%，差异登记待核对 |
| 游戏常量 | `calc/constants.py`，逐个常量可溯源到 GGG 原始表；**缺失即抛异常，不给默认值** |
| 受击结算 | `calc/mitigation.py` + `mitigation_calc` 工具：闪避与命中率（怪物命中值取自库里的等级表）、格挡几率与上限、法术压制（减 **40%** 不是 50%）、护甲对某一击的减伤（**护甲不是固定百分比减伤**，不给伤害数值就没有答案）。**不含头目的护甲/闪避倍率** |
| 暴击 | `calc/crit.py` + `crit_calc` 工具：暴击率（基础 + 定值）×(1 + 增加)× 更多、夹在 **100%** 上限内（不是 95%），以及平均伤害倍率 =(1 − 暴击率)+ 暴击率 × 暴击加成。基础暴击按技能名或武器底材名查库。**只给倍率不给 DPS**（伤害数值未采集） |
| 命中与攻速 | `calc/accuracy.py` + `accuracy_calc`：命中率 = 取整(命中值 /(命中值 +(敌方闪避 / 5)^0.9)× 125)，夹在 **5%–100%**，再乘敌方格挡；敌方闪避按等级查表（83 级普通怪 7,860），也能反解 90% 命中所需的 1,899。`calc/attack_speed.py` + `attack_speed_calc`：武器基础攻速 ×(1 + 技能倍率)，再除以 round((1 + 加成%)× 更多, 2)，非引导技能最终按 `ServerTickRate × Repeats` 夹值。**都不给 DPS**，也不含头目闪避倍率与双持 |
| 单技能 DPS | `calc/dps.py` + `calc/dps_analysis.py`：覆盖基础/附加命中、增伤/更多、PoB 顺序转化、抗性/物理减伤、暴击/命中、幸运与倍伤、冷却、重复爆发、服务器 tick、战斗机制、动态护甲下的稳态穿刺、指定战斗时长的起手叠层时间线和终结；可选联算点燃/中毒/流血，以及非异常技能 DoT。技能 DoT 支持五种伤害类型、独立增伤/更多/DoT 加成、敌方减伤/承伤、不可叠层多实例与 `施加率 × 持续时间` 叠层，并在全部 DoT 汇总后应用全局上限。比较和目标频率同步支持十四种口径。**只接受显式输入；概率穿刺时间线是期望值模型，异常源伤与技能每秒基础 DoT 均不能从最终命中倒推，仍不冒充完整 Build DPS** |
| 多技能组合 DPS | `calc/dps_rotation.py` + `rotation_dps_*`：组合 2–10 条完整单技能链；`rotation` 模式把显式百分比分配回动作频率，`parallel` 模式表达图腾/召唤物等独立来源覆盖率。点燃与单层流血跨技能自动只取最强实例，技能 DoT 可声明 `strongest` 互斥组；所有分量组合后只应用一次全局 DoT 上限和顶层终结。固定窗口在 512 个对齐采样段逐时互斥/封顶后积分，并支持多方案比较与目标统一频率倍率反解。**不自动读取装备、天赋或 Build，不补走位、资源、阶段、动态快照与召唤物 AI** |
| 一发最大承受 | `calc/maxhit.py` + `max_hit_calc` 工具：把抗性 → 护甲/减伤 → 生命+护盾池反解，给出「一发最多能挨多少」与「相当于裸池的多少倍」。无护甲是一步除法，有护甲按 PoB 的二次方程闭式解。验证方式是**自洽回代**（算出来的值走正向链路必须正好打满池子）。**只算单一伤害类型、不转化**，不含守护技能 / Ward / Mind over Matter |
| 门槛型异常 | `calc/threshold.py` + `ailment_threshold_calc` 工具：感电 / 冰缓 / 焦灼 / 脆弱 / 干枯的强度 = 基数 ×(伤害 ÷ 异常门槛)^0.4；门槛按等级表 × 头目 7.68 × 地图倍率算（83 级小怪 32,017、头目 1,086,003）。也能反问「要感电到 X% 需要多大伤害」。**冰冻只给门槛判据、不算时长曲线** |
| 持续伤害 | `calc/ailment.py` + `ailment_dot_calc` 工具：点燃 / 中毒 / 流血的**每秒**伤害、持续与总伤害（点燃每秒 90%×4 秒 = 总共 360%），含叠加规则（中毒无上限）与减伤口径（**流血不吃护甲**）。**感电与冰冻是门槛型异常、不算** |
| 保留计算 | `calc/reservation.py` + `reservation_calc` 工具：按名字查库取每个光环的基础保留（**含定值保留**，第二十四轮补上的 400 行），套 PoB 的取整规则（各自 round 2 位 → 求和 → 只向上取整一次）与保留效率（除法），给出合计与剩余。**不含血魔法** |
| 回复计算 | `calc/recovery.py` + `recovery_calc` 工具：护盾回充（每秒量、**2 秒延迟**、多久回满）、偷取（单次量、持续时间、速率上限与**单次实例上限**两层）、魔力再生（基数每分钟 105%）。独立公式工具不读取 Build；显式击回/药剂/偷取窗口由 `boss_recovery_sequence_*` 结算，仍不自动生成事件 |
| 抗性计算 | `calc/resistance.py` + `resistance_calc` 工具：按 PoB 原式复算（加总 → 截断小数 → 夹在 −200% 与最大抗性之间），给出**逐步推导**、超出上限的部分、距上限差多少与承受倍率；与 PoB 自己的测试期望值对账，公式原文钉住上游行号。**图腾抗性与持续伤害抗性未实现**，也不读取角色档案 |
| 混合检索 | 实体精确匹配 + BM25（FTS5 + jieba）+ 词缀意图通道，RRF 融合 + 来源优先级加权 |
| 充分依据判定 | 依据不足时返回 `sufficient=false` 与缺失信息点，**不**进入生成 |
| **问答** | `POST /chat`：强制检索 → 充分性判定 → 生成 → 出口绑定校验 → 装配引用 |
| **流式问答** | `POST /chat/stream`：SSE，**按句**校验通过才下发，未校验内容不外发 |
| **OpenAI 兼容** | `POST /v1/chat/completions`：支持文本消息、非流式与标准 SSE `[DONE]`；内部仍强制检索与出口校验 |
| **用户长期记忆** | Web 顶栏或 `POST/GET/PATCH/DELETE /memories` 可显式管理偏好、目标、约束与备注，支持编辑、停用、恢复和删除；按 API Key 的不可逆摘要隔离，**不自动从聊天抽取、不成为知识库证据** |
| **MCP stdio** | `mcp_server.py`：按 MCP `2025-06-18` 向 Codex、Claude Desktop 等客户端暴露 Knowledge Agent 的 81 个只读工具；支持 `--tools` 白名单，工具结果保留结构化数据、引用、unknown/boundary 与耗时 |
| **知识库自动同步与版本 diff** | `scripts/sync_knowledge.py`：按最后成功采集时间自动选择过期分类，复用现有结构断言与快照数量护栏；同步前后按稳定主键报告逐表新增/删除/修改与版本字段变化，历史可从 `/admin/sync/*` 查询。默认只预览，记录删除时整轮标记 `attention` |
| **一致性备份与离线恢复** | `scripts/db_backup.py` 使用 SQLite Backup API 从活跃 WAL 连接创建快照，清单固定 SHA-256、schema 指纹、关键表行数与 `quick_check`；恢复默认只预检，拒绝存在 WAL/SHM 的目标，`--apply` 前自动留存当前库。管理端只开放创建、列表和校验，远程恢复刻意不提供 |
| **鉴权与限流** | 普通接口使用 `API_KEYS` 并按 Key 令牌桶限流；管理接口独立使用 `ADMIN_TOKEN`，可叠加 IP/CIDR 白名单 |
| **出口闸门** | 逐句校验事实断言与计算派生是否绑定有效证据，未通过由代码拦截；47 项单测覆盖 |
| **聊天界面** | `static/` 单页前端，无构建步骤、无 Node、无第三方 JS；可配置标签页级 API Key，并直接管理会话与显式长期记忆 |
| **会话历史** | `GET /conversations` 从 SQLite 恢复当前调用方最近会话，可读取、重命名和删除；会话按 API Key 的不可逆摘要隔离，已鉴权缓存也按当前标签页隔离 |
| **装备解析** | 游戏内 Ctrl+C / Ctrl+Alt+C 文本 → 结构化装备；中英文都认 |
| **词缀 T 级** | 按底材类别 + 数值区间定档；查不到就标「未匹配」并说明原因 |
| **装备分析** | `POST /item`：基础信息、词缀档位、前后缀容量、可制作方向，全程不调 LLM |
| **Build 体检、成长规划与方案比较** | PoB 分享码、白名单托管链接、官方天赋树、GGG OAuth 角色 API 或截图 → 核心体检、防御来源、输出分析、双档建议与显式目标成长里程碑；`POST /build/compare` 可在严格相同的 Calcs 技能、配置、树版本和 PlayerStat 字段口径下比较 2–5 份 PoB；`POST /planner/league-starter` 把当前 PoB 与 1–5 份用户提供的阶段快照排成可复核路线，网页有对应入口。截图先经视觉模型转录并由用户确认；缺失或不可比项保持「未知」 |
| LLM 客户端 | 固定 DeepSeek 网关调用，同模型多 Key 故障转移、分级冷却、文本/工具/图片共用客户端 |
| 缺口台账 | `knowledge_gap` 按频次排序即采集优先级；**评测流量与永久越界（POE2）不计入**（否则前两名会是本该拒答的黄金用例），`/admin/stats` 同时列出被排除的条目数 |
| 执行轨迹 | 每轮落库，含拦截原因与 Token 明细，`/admin/traces` 可追溯 |

**还没实现**

- 稀有装备的精确成交价/制作价值与按 roll、品质、插槽、链接做价格归一化（已有逐词缀同类挂单比较，品质与连接组尺寸可选精确筛选；「值不值得裂界」仍输出「数据不足」）
- 多步制作路线的真实单次成功率、逐步期望成本和预算降级；现有状态机和普通池独立尝试模型没有足够的联合概率来源。
- 自动生成的最优开荒阶段路线、Atlas 精确点数与最优配点、Boss 药剂及推荐 DPS 门槛、版本改动的语义判读和 Build 实际收益测算；用户 PoB 阶段路线与现有原文、候选路径接口保留这些缺口。
- 伤害转化下的 Max Hit 与冰冻时长曲线。完整 PoB 伤害引擎重写属于规格明确的非目标。

也就是说：**打开浏览器就能聊，也能直接把装备贴进去**，交互方式见下。

---

## DeepSeek 单模型配置

所有文本问答、流式生成、工具续答和截图转录统一使用 `deepseek-v4-1-flash-1017`。
模型真源是 `core/model_policy.py`；旧的其他模型别名、独立视觉模型或逐 Key 绑定模型
会在联网前被拒绝。多 Key 仅用于同一模型的凭证故障转移。

升级时在本机 `.env` 设置 `LLM_MODEL=deepseek-v4-1-flash-1017`，并填入自己的
`LLM_BASE_URL` 与 `LLM_API_KEY`。如使用 `LLM_API_KEYS`，请改为逗号分隔的 Key；
检查 `.env.local` 与环境变量是否仍覆盖了旧值。网关地址和 Key 不进入版本库。
`config.yaml` 删除了旧模型别名表和 `gate_model` / `vision_model`；截图开关改为
`llm.vision_enabled`，默认开启并复用同一模型。

远程 Embedding / ReRank 已禁用；旧配置无法重新开启，旧向量索引保留。
检索继续使用 BM25、实体精确匹配与关键词。`scripts/backfill_vectors.py --apply`
会在修改数据库前拒绝。`scripts/probe_gateway.py` 只使用本项目配置验证固定模型，
不再读取其他工具或项目的凭证，也不再允许命令行改模型/改网关。

流式问答只有收到完整结束信号才保存为会话答案。模型中断、输出截断或用户取消时关闭上游流，
丢弃未完成缓冲；已经收到的 token 用量仍计入本轮预算。网页端与 OpenAI 兼容接口统一报告失败。
POE2 边界声明、缺据声明等确定性路径不创建 LLM 客户端。

历史黄金录制中的模型标识是原始证据，继续保留；它们只用于离线回放，不参与运行时路由。

## 快速开始

需要 Python 3.12（最低 3.10），推荐 python.org 官方版或 Homebrew 版。检索不依赖 SQLite 向量扩展。

```bash
# 1) 初始化：建 .venv、装依赖、建 data/ 与 logs/、从模板生成 .env
python scripts/setup.py          # Windows: python scripts\setup.py

# 2) 填 .env（至少这两项）
#    LLM_BASE_URL=<你的 LiteLLM 网关地址>
#    LLM_API_KEY=<virtual key>
#    ADMIN_TOKEN=<自己起一个，管理端点用，别和用户 Key 共用>
#    POE_OAUTH_TOKEN=<可选；角色导入需要 account:characters scope>
#    POE_OAUTH_USER_AGENT=<OAuth clientId/version (contact: 联系方式)>

# 3) 自检：确认运行环境、备份目录就绪，并检查跨平台代码约束
.venv/bin/python scripts/selfcheck.py
.venv/bin/python scripts/check_portability.py

# 4) 建知识库（首次会抓 POEDB，之后走本地缓存）
.venv/bin/python -m kb.pipeline --category all
#    可单独跑：unique_item / unique_l2 / mods / bench（工艺台目录）
#              / horticraft（收获定向工艺）/ metamods（元词缀矩阵）
#              / tree（天赋树与 Atlas）/ coverage（只刷覆盖声明）
#    首次完成唯一装备详情补全后，后续刷新请用下文的完整刷新命令；
#    主列表单独导入会被拒绝，防止覆盖详情页补出的词条。

# 4b) 可选：预检并发布官方补丁公告与 Wiki Boss 攻略
.venv/bin/python scripts/import_ggg_patch_notes.py --pages 1
.venv/bin/python scripts/import_ggg_patch_notes.py --pages 1 --apply
.venv/bin/python scripts/import_boss_guides.py
.venv/bin/python scripts/import_boss_guides.py --apply

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
Knowledge Agent 的 81 个只读工具；需要缩小能力面时传入逗号分隔的白名单：

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

### 自动同步知识库并审查版本变化

默认命令只生成计划，**不联网、不改事实表**。配置中的 `stale_after_hours` 默认为 168
小时；只有显式加入 `--apply` 才执行到期分类：

```bash
# 查看哪些分类已经过期
.venv/bin/python scripts/sync_knowledge.py

# 执行所有到期分类；适合由系统定时器调用
.venv/bin/python scripts/sync_knowledge.py --apply --json

# 人工指定分类，并忽略 HTTP 缓存重新抓取
.venv/bin/python scripts/sync_knowledge.py \
  --apply --categories currency,maps --force
```

同步直接复用 `kb.pipeline` 已有的 robots.txt、限速、结构断言、数量缩减护栏与原子提交，
不会绕开采集安全边界。每个分类执行前后都会按稳定主键比较事实行，区分 `added`、
`removed`、`changed`、`unchanged`，并单列 `version`、`game_version`、`source_commit`、
`tree_version` 等版本标记。只要出现删除，命令就以 `attention` 结束并返回非零状态，提醒
人工核对；真实上游删项不会被程序凭数量猜成错误并自动回滚。

管理端提供只读查询：`GET /admin/sync/plan`、`GET /admin/sync/runs`、
`GET /admin/sync/runs/{sync_id}`，继续受独立 `ADMIN_TOKEN` 与可选 IP 白名单保护。

当前自动集合覆盖词缀、工艺台、元词缀矩阵、天赋树、通货、地图、圣甲虫、精华、收获、
怪物、GGG 官方公告和 Wiki Boss 攻略。唯一装备使用独立的完整刷新流程：

```bash
# 只读状态与本地缓存预检；预检不会修改数据库或发网络请求
.venv/bin/python scripts/refresh_unique_items.py status
.venv/bin/python scripts/refresh_unique_items.py preview
.venv/bin/python scripts/refresh_unique_items.py proof --slug Headhunter

# 重新抓取主列表、类别页、英文名页及每件详情页，完整预检后备份并原子发布
.venv/bin/python scripts/refresh_unique_items.py apply
```

完整刷新约 1,450 页；在默认每秒 0.5 次请求的限速下，可能需要约 50 分钟。
`preview --fetch` 可做在线只读预检，但 `apply` 仍会独立重新抓取，避免以旧预检发布。
缓存预检会显示各页来源时间，**不能当成当前上游状态**。若列表与详情不一致，
流程保留该物品已有实体与卡片，并登记 `unique_review` 待核对；缺页、解析失败、
主列表/类别补集/英文索引低于完整性下限或清单骤降
则整轮中止。发布前创建 SQLite 一致性备份，实体、卡片、别名、审计在同一事务中提交。
`proof` 可查询已发布刷新中某件装备详情页的最终 URL、内容哈希、抓取时间与缓存标记；
每页来源账与聚合摘要一起落库。`status` 会重算实体、卡片、FTS、别名及来源账摘要，
显示 `valid` 或 `drifted`，便于发现发布后的变更或审计损坏。
已有详情补全记录的库会拒绝旧的 `--category unique_item` 单页导入，防止详情词条回退。
独立刷新尚未加入默认定时同步：全量详情页的时间和请求量需要单独安排维护窗口。

可选：把 PoB 数据也导进来（需要本地有 PathOfBuilding 仓库）

```bash
# 仓库位置按 POB_PATH 环境变量 → config.yaml 的 sources.pob.path
# → 本仓库同级目录 ../PathOfBuilding 依次解析
.venv/bin/python scripts/probe_pob.py --align       # 先清点，确认解析无误
.venv/bin/python scripts/import_pob_constants.py    # 游戏常量（calc/ 的真源）
.venv/bin/python scripts/import_pob_mods.py         # 词缀
.venv/bin/python scripts/import_pob_uniques.py      # 当前装备 + 隔离的逐变体历史（先备份、原子发布）
.venv/bin/python scripts/verify_pob_mods.py         # 词缀对账
.venv/bin/python scripts/verify_pob_uniques.py      # 唯一装备对账
```

导入唯一装备时会自动过滤历史变体：1300 件里 758 件带 `Variant:` 标记，
不过滤会把旧版数值和现版一起输出（冈姆的壮志的 `+500` 与 `+1000` 并存）。
判据是**排除变体名含 `Pre <版本号>` 的**，而不是「只取 Current」——有些变体
是选择型而非历史版本（`Impresence` 的 5 种元素全都当前有效）。
导入会在完整性体检通过后备份数据库，并在同一事务发布当前 `pob_unique` 与隔离的
`pob_unique_variant`。PoB 当前样本有 1,300 件装备、2,642 条命名变体记录，其中
1,493 条是历史变体。旧版词缀不进入当前装备卡片、别名或全文索引。

历史查询走 `GET /poe_agent/unique-variants?name=Kaom%27s%20Heart`；返回变体编号后，
`GET /poe_agent/unique-variants/detail?name=Kaom%27s%20Heart&index=2` 读取指定变体，
`GET /poe_agent/unique-variants/compare?name=Kaom%27s%20Heart&left_index=2&right_index=3`
比较指定两版的共同、新增、移除词缀和底材。Knowledge/MCP 工具
`unique_variant_history` 也使用同一只读查询。`Pre 3.25.0` 的意思是 **3.25.0 之前**，
不是 3.25.0 的精确快照；多个当前选择型变体必须明确指定编号，未知历史底材保持未知。

网页聊天页底部的“浏览唯一装备历史变体”可直接按中文名或英文名搜索归档目录，默认只列
含历史变体的装备；取消勾选后也能浏览当前选择型变体。结果按英文名与 UID 稳定排序，
每页 20 件。选中装备后要手动选择第一个变体；第二个编号留空时显示完整词缀，选择后
显示两侧完整词缀及共有、仅左侧、仅右侧的差异。`Pre X` 边界、未知底材和钉住 commit
的来源链接在页面上直接展示，不依赖模型转述。程序入口为
`GET /poe_agent/unique-variants/search?query=Kaom&historical_only=true&limit=20&offset=0`；
`%`、`_` 和反斜杠在搜索词中按字面处理，响应包含总数、是否还有下一页及归档是否就绪。

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
- **正文出现前会显示「思考中」**。DeepSeek 的推理字段可能缺失或为零；如果返回了
  推理内容，也只统计用量和字符数，绝不作为答案外发或写入轨迹。
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

左上角可切换历史会话（匿名模式列表缓存于 `localStorage`，鉴权模式缓存于当前标签页，
消息与标题以 SQLite 为准），也可重命名当前会话；点「新对话」重开。

### 方式二：浏览器打开交互式 API 文档

```
http://127.0.0.1:8787/poe_agent/docs
```

FastAPI 自带的 Swagger UI，可以填参数、点 **Try it out** 直接发请求、看完整响应。
调 `/search`、`/admin/*` 这类前端没暴露的接口时用它最省事。

`http://127.0.0.1:8787/poe_agent/redoc` 是同一套接口的另一种阅读视图。

### 方式三：命令行调接口

**单技能 DPS** —— 只用调用方明确提供的数值走确定性主链，响应包含逐伤害类型区间、
有效抗性/减伤、暴击与命中期望、战斗机制倍率、命中 DPS、动态护甲下的稳态穿刺、可选固定时长
穿刺与持续伤害统一时间线、伤害性异常与技能 DoT、
终结等效输出及边界：

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/calc/hit-dps \
  -H 'Content-Type: application/json' \
  -d '{
    "base_damage":{"fire":[100,200]}, "action_rate":2,
    "increased_damage_pct":100, "more_damage_multipliers":[1.2],
    "hit_chance_pct":90, "crit_chance_pct":50, "crit_multiplier_pct":300,
    "enemy_resistance":{"fire":50}, "penetration":{"fire":20}
  }'
```

上述输入的 `dps` 为 `907.2`。复杂转化可在 `conversions` 中逐条给出 `source`、
`target`、`percent` 与 `kind`（`skill` / `global` / `gain`）。

进阶参数包括 `server_tick_seconds` / `cooldown_uses` / `action_repeats` / `channelled`、
`ruthless_blow_*`、`fist_of_war_*`、`exerted_attack_*`、`impale_*`、`enemy_armour`、
`combat_duration_seconds` 与两种 `cull`。
服务器 tick 只在明确给值时进入 DPS 链；单充能冷却向上对齐 tick，多充能跳过冷却对齐，
非引导技能频率夹在 `重复次数 / tick 秒数`。

穿刺稳态沿用 PoB 配置层数口径：`单层储存比例 × 层数 × 减伤前物理平均击中` 先进入敌方
护甲公式，再与固定物理减伤合计并夹在 90%。显式给出 `combat_duration_seconds` 时还会按
有效命中频率生成起手层数、逐阶段护甲减伤、可持续层位与窗口平均穿刺 DPS；100% 命中且
100% 穿刺时是逐击爬坡，概率不足 100% 时是与 PoB 稳态公式一致的期望值延伸。

同一个 `combat_duration_seconds` 还会生成完整伤害时间线：点燃/流血按截至当前时刻仍活跃的
候选实例重算强实例 roll，中毒和可叠层技能 DoT 按施加率起手并在持续时间后逐层到期；
不可叠层技能 DoT 按刷新频率与持续时间计算覆盖率。每个时点先应用各异常/技能 DoT 的独立
上限，再对合计应用全局 DoT 上限，最后与命中和动态穿刺合并。结果单列
`combat_average_ailment_dps`、`combat_average_skill_dot_dps`、
`combat_average_total_dot_dps`、`combat_average_total_dps` 与含终结口径。时间维度采用固定输入
快照，并为各异常/技能 DoT 返回平均活跃实例、到达稳态时间和起手损失；若触及全局上限，
还会报告首次封顶时间与窗口封顶覆盖率。战斗途中 buff/debuff 改变后的重新快照、特殊地面
效果互斥仍不在本轮范围内。

`damaging_ailments` 最多分别提供一组 `ignite` / `poison` / `bleed`。每组必须显式给出
异常专用 `source_damage`；还可给非暴击/暴击施加率与源伤、DoT 加成、持续时间、加速、
层数上限和独立减伤。计算器不会把最终命中伤害倒推成异常源伤，因为穿透、双倍/三倍伤害
及仅击中生效的修正不应进入异常链。结果单列施加率、持续时间、稳态层数、强实例 roll、
单实例/稳态 DPS、是否触及各项与全局 DoT 上限，以及命中+穿刺+持续伤害总计。

`skill_dot` 接受技能自身每秒基础持续伤害，不要求同时存在命中：

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/calc/hit-dps \
  -H 'Content-Type: application/json' \
  -d '{
    "action_rate":2,
    "skill_dot":{
      "base_damage_per_second":{"fire":1000,"chaos":500},
      "increased_damage_pct":50,
      "more_damage_multipliers":[1.2],
      "dot_multiplier_pct":20,
      "can_stack":true,
      "duration_seconds":2
    },
    "enemy_resistance":{"fire":75,"chaos":20}
  }'
```

逐类型按 `基础每秒 × 增伤 × 更多 × DoT 加成 × 光环效果 × 敌方有效承伤` 计算；元素/混沌
持续伤害吃抗性但不吃命中穿透，物理持续伤害吃怪物物理减伤但不吃护甲、压倒或概率忽略。
不可叠层默认只算一个实例，可用 `instance_multiplier` 显式表达多个附着烙印等同时生效实例；
可叠层则按有效动作频率、`application_rate_multiplier`、持续时间、技能 DPS 与数量倍率计算
稳态实例数，并可用 `stack_limit` 截断。技能 DoT 与伤害性异常合计后再受 PoB 全局 DoT 上限约束。

同一模型下比较 2–10 套输入使用 `/calc/hit-dps/compare`，同时排列命中、命中+穿刺、异常、技能 DoT、全部 DoT、
总计及含终结等效 DPS 与一轮爆发。`/calc/hit-dps/target-rate` 的 `metric` 可选 `hit_dps`、
`combined_dps`、`effective_dps_with_cull`、`ailment_dps`、`skill_dot_dps`、`total_dot_dps`、`total_dps` 或
`effective_total_dps_with_cull`、`combat_average_combined_dps`、`combat_average_ailment_dps`、
`combat_average_skill_dot_dps`、`combat_average_total_dot_dps`、`combat_average_total_dps` 或
`combat_average_effective_total_dps_with_cull`；反解使用同一主链二分回代，可处理强实例取样、稳态层数、
DoT 上限、战争之拳覆盖率、暴击终结和 tick/冷却平台，不假设 DPS 与频率永远线性。

**多技能组合与轮转** —— `POST /calc/rotation-dps`。每个 `skills[].inputs` 都复用上面的
完整单技能参数；组合层不会复制一套较简化的伤害公式。共享角色动作时间时使用
`mode=rotation`，并显式分配 `allocation_pct`（总和不得超过 100%）：

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/calc/rotation-dps \
  -H 'Content-Type: application/json' \
  -d '{
    "mode":"rotation",
    "combat_duration_seconds":8,
    "cull_percent":20,
    "skills":[
      {
        "name":"点燃主技能",
        "allocation_pct":70,
        "inputs":{
          "base_damage":{"fire":[100,200]},
          "action_rate":2,
          "damaging_ailments":[{
            "name":"ignite",
            "source_damage":[500,900],
            "chance_on_hit_pct":100
          }]
        }
      },
      {
        "name":"燃烧地面",
        "allocation_pct":30,
        "inputs":{
          "action_rate":1,
          "skill_dot":{
            "base_damage_per_second":{"fire":600},
            "duration_seconds":4
          }
        }
      }
    ]
  }'
```

动作分配在进入单技能链之前就乘到原始 `action_rate`，所以冷却、服务器 tick、穿刺起手、
异常候选数、可叠层 DoT 与刷新覆盖都会按分配后的真实频率重算，而不是把两个稳态面板
最后乘百分比。`mode=parallel` 用于图腾、召唤物或其它独立来源；此时 `allocation_pct`
表示该来源的显式覆盖率，省略时为 100%，各来源不共享 100% 动作预算。

跨技能持续伤害按分量组合：

- 默认单层点燃与单层流血会自动建立 `strongest` 组，同一时点只保留最强来源；显式多层
  流血不会被错误压成一层。
- 中毒默认跨技能相加。不可叠加的技能 DoT、同名燃烧地面等必须由调用方用 `dot_groups`
  指明成员，计算器不会仅凭名字猜游戏机制。
- 每个单技能分量先应用自己的独立上限，互斥组再逐时选择最强来源；全部保留分量合计后，
  整套组合只应用一次全局 `DotDpsCap`。顶层 `cull_percent` 同样只应用一次，单技能 inputs
  中的终结字段会被拒绝，避免重复计算。

显式互斥组示例：

```json
{
  "dot_groups":[{
    "name":"燃烧地面",
    "policy":"strongest",
    "members":[
      {"skill":"火墙","component":"skill_dot"},
      {"skill":"灼热地面","component":"skill_dot"}
    ]
  }]
}
```

固定战斗时长下，每个技能在同一组 512 段时间点上生成命中、穿刺和各 DoT 分量；组合层在
每个时点完成互斥与全局封顶后再用 Simpson 积分。这样“两个技能各自的窗口平均值相加”不会
掩盖强实例来源切换或只在部分窗口触顶。响应会返回每个技能的分配后/最终有效频率、各 DoT
归属组、稳态主导来源、窗口中主导来源切换次数、组合时间线和稳态/窗口平均总 DPS。

多套轮转方案同口径比较使用 `POST /calc/rotation-dps/compare`。整套组合保持分配比例不变、
统一提高或降低所有技能的配置动作频率时，使用
`POST /calc/rotation-dps/target-multiplier`；支持稳态/固定窗口的总 DPS、全部 DoT 和含终结
口径，并由完整组合链二分回代，而不是用目标值除以当前 DPS。战争之拳覆盖随频率变化时
可能非单调，因此该反解会保守拒绝。

多技能组合仍是**显式输入模型**：它不会从装备、天赋、宝石链接或 PoB Build 自动生成各
技能参数，也不推断轮转分配；走位损失、魔力/怒火等资源耗尽、Boss 阶段、动态 buff/debuff
重新快照、召唤物 AI 与未声明的特殊来源仍保持未知。

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
正常检索使用 `alias`、`fts`、`mod_intent`，不会因为未启用远程模型而显示降级提示。

引用
[E1] 来源：POEDB → 猎首（Headhunter）
     https://poedb.tw/cn/Headhunter
[E2] 来源：POEDB → 猎首【仿品】（Replica Headhunter）
     https://poedb.tw/cn/Replica_Headhunter
```

对外提供服务时应在 `.env` 配置逗号分隔的 `API_KEYS`，并在普通业务请求中发送
`Authorization: Bearer <key>`。`config.yaml` 的 `server.rate_limit` 默认允许每个 Key
持续 60 次/分钟、最多突发 10 次；超限返回 HTTP 429 和 `Retry-After`。未配置
`API_KEYS` 的本机开发模式无需鉴权，但所有请求共用一个 `anonymous` 限流桶和长期记忆
命名空间。因此匿名模式只适合本机单用户；多用户或对外部署必须配置 `API_KEYS`。
Web 页可用顶栏“API 密钥”按钮验证并配置 Key；值只写入 `sessionStorage`，关闭标签页即
清除，不写 `localStorage`、聊天记录或日志。服务端返回 401 时页面会清掉已拒绝的 Key 并
重新打开密码输入框；已鉴权的会话标题缓存同样只在当前标签页保存，“退出并清除”还会清除
该标签页显示的会话状态，避免切换用户或并行使用不同 Key 时串线。

**服务端会话历史** —— 聊天、流式聊天与 OpenAI 兼容入口创建的会话都会绑定当前调用方。
列表默认返回最近 30 条，含消息数与末条消息摘要；读取、续聊、重命名和删除均校验同一
调用方，不会通过响应暴露其他调用方是否持有某个会话 ID：

```bash
# 最近会话
curl -s 'http://127.0.0.1:8787/poe_agent/conversations?limit=30' \
  -H "Authorization: Bearer $POE_AGENT_API_KEY"

# 读取、重命名、删除；把 <conversation_id> 换成列表中的值
curl -s http://127.0.0.1:8787/poe_agent/conversations/<conversation_id> \
  -H "Authorization: Bearer $POE_AGENT_API_KEY"
curl -s -X PATCH http://127.0.0.1:8787/poe_agent/conversations/<conversation_id> \
  -H "Authorization: Bearer $POE_AGENT_API_KEY" \
  -H 'Content-Type: application/json' -d '{"title":"火花配装复盘"}'
curl -s -X DELETE http://127.0.0.1:8787/poe_agent/conversations/<conversation_id> \
  -H "Authorization: Bearer $POE_AGENT_API_KEY"
```

Web 页启动时会与服务端同步列表，刷新、清缓存或换浏览器后仍能恢复会话。升级前创建的
旧会话没有所有者信息，不会直接进入任何人的服务端列表；仍持有原随机 ID 的浏览器可在
首次读取或续聊时将它认领到当前调用方，避免用不可靠信息猜测旧数据归属。

**用户长期记忆** —— 只有调用方通过 API 明确提交时才保存，不会从聊天、装备或 PoB
内容自动抽取。类别固定为 `preference`、`goal`、`constraint`、`note`；单个调用方最多
100 条，单条正文最多 500 字。Web 顶栏“长期记忆”提供完整列表、创建、编辑、停用、恢复
和确认删除；列表包含停用项，所有用户正文均以纯文本节点渲染。API 列表默认只返回活跃项：

```bash
# 保存
curl -s -X POST http://127.0.0.1:8787/poe_agent/memories \
  -H "Authorization: Bearer $POE_AGENT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"category":"preference","label":"表达","content":"先给结论，再说明依据"}'

# 查询；加 include_inactive=true 可同时查看已停用项
curl -s 'http://127.0.0.1:8787/poe_agent/memories?include_inactive=true' \
  -H "Authorization: Bearer $POE_AGENT_API_KEY"

# 修改或停用；把 <memory_id> 换成创建响应中的值
curl -s -X PATCH http://127.0.0.1:8787/poe_agent/memories/<memory_id> \
  -H "Authorization: Bearer $POE_AGENT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"active":false}'

# 永久删除
curl -s -X DELETE http://127.0.0.1:8787/poe_agent/memories/<memory_id> \
  -H "Authorization: Bearer $POE_AGENT_API_KEY"
```

后续 `/chat`、`/chat/stream` 和 `/v1/chat/completions` 会在真正进入模型生成前注入同一
调用方最近的活跃记忆，最多 20 条且整段不超过 4000 字符。它们被明确标记为不可信用户
数据：只能个性化表达、建议目标与约束，当前问题始终优先；不能支撑游戏机制、数值、价格
或版本事实，也不会获得 `[[E*]]` 编号。缺据拒答和确定性工具路径不读取记忆，记忆库故障
也不会阻断核心问答。

句末的 `[[E1]]` 是**证据绑定标记**，不是装饰。出口闸门会逐句检查：

- 事实句必须绑定证据编号，未绑定即拦截
- 句中出现的每个数值必须能在它绑定的证据原文里找到（证据给区间 `(40-55)`
  时，句中取具体值 `47` 算合法）
- 编号必须有效，来源优先级必须能支撑事实断言（P7 社区来源不行）
- 禁止「一般来说」「大概」这类模糊限定语

未通过的句子被替换为拦截说明，并计入响应的 `blocked_claims`。这个字段大于 0
说明模型试图输出无据内容而被挡下，可用 `trace_id` 查具体原因。

响应里的 `model` 字段固定为实际请求的 `deepseek-v4-1-flash-1017`；多 Key 故障转移不切换模型。

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

聊天页底部的「经济分析」可直接使用以下四项能力：当前价格、历史趋势、明确材料清单成本、
双边价差。表单只在点击查询后请求对应接口；联盟可留空，材料按行输入名称、数量与类别，
双边价差要求明确填写数量、挂单时限和买卖两侧费用（确认为零时填 `0`）。结果会逐项展示
来源、时间、缓存/降级状态、样本范围与理由；样本不足或任一材料缺价时不会把部分信息
冒充完整趋势、总价或利润。API Key 失效或切换后，面板会清除未提交输入与旧结果。

**实时价格** —— `POST /economy/price`，按精确名称查询当前或指定联盟；中文唯一装备、
通货与圣甲虫名会先经本地权威别名解析。正常响应中的 `mean_chaos` / `min_chaos` /
`max_chaos` 是 poe.watch 统计快照。若 poe.watch 请求失败且本地完全没有可用缓存，标准
通货、圣甲虫、命运卡和碎片会显式降级到 GGG Trade API exchange；唯一装备则用本地
权威库确定的英文名和底材查询官方 search/fetch，并只取未腐化结果。服务按请求联盟过滤
在线账号、校验物品身份与报价字段，将 Divine 标价用同一次备用链的 exchange 中位数换算
成 Chaos，再使用 Tukey 1.5×IQR 过滤明显离群价。两种来源都不是保证成交价。每次
poe.watch 精确命中会按快照抓取时间写入历史；备用挂单不混入 poe.watch 历史序列。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/economy/price \
  -H 'Content-Type: application/json' \
  -d '{"query":"猎首","category":"auto"}'
```

响应始终带 `league`、`source`、`fetched_at`、`from_cache`、`stale`、`degraded`、
`fallback`、`fallback_reason` 和 `warning`。备用响应额外披露原始/保留样本数、最低样本
门槛、跨联盟/无效/离群过滤数，以及最早和最新挂单时间；过滤后不足 5 条时保留这些审计
信息，但不输出代表价。唯一装备额外披露官方总命中数、本次抓取数、Chaos/Divine 标价
构成、Divine 换算样本，以及“未按词缀数值、插槽或链接归一化”的样本边界；本地缺少
精确底材时返回 **503**，不会只按名字或相似物品猜价。

**稀有装备可比挂单** —— `POST /economy/rare-comparables`，输入完整装备复制文本。
聊天页底部的「查询稀有装可比挂单」可直接打开网页查询；粘贴稀有装备时，确认卡片也有
「查询可比挂单」按钮，只预填复制文本，仍需点击查询才访问 GGG Trade。网页可填联盟、
勾选品质与连接组尺寸精确匹配，并展示词缀映射、代表价或样本不足原因、抓取时间、
过滤审计和官方搜索链接。API Key 变更时会清空已有结果与未提交的装备文本。
服务先用本地 `pob_base_item` 把中英文底材唯一映射为官方英文底材，再把显式词缀经
PoB `tradeHashes` 映射并用 GGG 当前 `/api/trade/data/stats` 二次校验。查询限定当前或
指定联盟、在线、稀有、精确底材、相同物等和腐化状态；fetch 后再次校验这些身份字段。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/economy/rare-comparables \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{"item_text":"物品类别: 项链\n稀有度: 稀有\n恐惧之瞳\n玛瑙护身符\n--------\n物品等级: 84\n--------\n+107 最大生命\n+43% 火焰抗性"}
JSON
```

每条已映射/未映射词缀都会返回；未映射词缀存在时结果标为 `partial`。中文普通复制
中已核实的相邻多行混合词缀按**一条**计数，并把整条对应的 Trade stat 一起送入筛选。
可选 `match_quality=true`、`match_sockets=true`：复制文本须明确给出对应字段；fetch 后
逐件核对整数品质与连接组尺寸（如 `R-G B-B-B` 为 `[2,3]`），挂单字段缺失或不符便剔除，
并单列过滤计数。只比较连接组大小，**不比较颜色或位置，也不按差异调价**。未启用时
保留原有较宽样本范围。查询按价格升序最多抓取前 40 条，结构筛选只作用于这批挂单；
样本不足并不证明整个市场没有可比物品。stat 仍只约束词缀存在，不归一化数值 roll、隐式或附魔；
特殊影响力、合成、裂隙、镜像和分裂状态在建立对应官方筛选契约前直接拒绝。
过滤后不足 5 条不输出代表价。即便样本足够，
结果也只称“可比挂单中位数/范围”，不据此判断精确成交价、制作价值、利润、是否值得裂界、
镜像湖或镜子级别。

**历史价格趋势** —— `POST /economy/trend`。优先比较本机实际保存的带时间戳快照；默认
查看 168 小时，可传 1–2160 小时。默认七日窗口本机点数不足时，可使用 poe.watch 明确
返回的 `sevenDaysHistory` 数列，但会标注上游没有提供逐点时间戳。其他窗口至少需要两个
不同抓取时间的有效均价，否则 `analyzable=false` 并明确说明观测不足。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/economy/trend \
  -H 'Content-Type: application/json' \
  -d '{"query":"猎首","category":"auto","window_hours":168}'
```

趋势返回首尾均价、绝对/百分比变化、窗口内范围与序列口径。本机历史的计算使用窗口内
全部记录，结构化响应最多等距返回 200 个点；无论哪种口径都不把 poe.watch 单次响应的
`change` 字段冒充多日历史，也不据此预测未来。

**明确材料清单成本** —— `POST /economy/material-cost`。每项都必须给名称和正数数量；
服务逐项精确匹配，并用同一联盟的代表价与观测区间乘以数量做确定性加总；代表价口径会
逐项标成 poe.watch mean、GGG 在线挂单中位数或混沌石计价单位恒等式。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/economy/material-cost \
  -H 'Content-Type: application/json' \
  -d '{"materials":[{"query":"神圣石","quantity":2},{"query":"混沌石","quantity":100}]}'
```

若任一材料未找到、同名结果不唯一或缺少代表价，`calculable=false` 且代表价总计为空，绝不会把
已查到的部分冒充完整总成本。它只计算输入清单，不会反推某件装备需要哪些材料、成功率或工艺步骤。

**双边价差与利润** —— `POST /economy/flip-analysis`。仅支持 GGG exchange 字典中的标准
通货、圣甲虫、命运卡和碎片；唯一/稀有装备没有可验证的双边 exchange 深度，不进入计算。
请求必须明确数量及买卖两侧固定/比例费用（无费用也显式填 `0`），可再指定最低净利润、
最低利润率和 1–168 小时挂单时限。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/economy/flip-analysis \
  -H 'Content-Type: application/json' \
  -d '{"query":"神圣石","quantity":10,"fees":{"buy_fixed_chaos":0,"buy_percent":0,"sell_fixed_chaos":0,"sell_percent":0},"minimum_net_profit_chaos":20,"minimum_margin_percent":5}'
```

服务分别查询“花 Chaos 买入目标物品”的卖家要价和“卖出目标物品换 Chaos”的买家承接价，
统一为 Chaos/个。每条保留挂单必须在线、属于目标联盟、在时限内，并且自身库存能覆盖整笔
目标数量；两侧还要各自达到最低样本数。只有全部条件成立时才返回毛成本、毛收入、逐侧费用、
净利润、利润率和 `flip_candidate / not_profitable`。任一侧样本、数量深度或时效不足时，
`status=unknown`、`verdict=unknown`，所有利润字段为 `null`，不会拿单边价格拼出套利建议。
`flip_candidate` 只表示当前在线挂单中位数达到调用方阈值，未计入未填写的时间成本、议价、
滑点、重复账号或查询后的行情变化，不是成交或收益保证。

**Boss 同一命中重复校验** —— `POST /boss/survival`。技能名与等级决定 PoB 建模的
原始伤害区间，用户明确提供的防御池和对应抗性进入 Max Hit 链路；技能自带的普通/Uber
元素穿透会在结算前扣除。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/boss/survival \
  -H 'Content-Type: application/json' \
  -d '{"skill":"Shaper Ball","level":84,"pool":10000,"hit_count":2,"cold_resist_pct":75}'
```

返回 `survives_full_range / roll_dependent / fails_full_range` 三态，并同时给出每击与累计原始伤害、
每击与累计承伤、剩余池子、同口径 Max Hit，以及最坏/最好掷骰下第几击耗尽。
`hit_count` 默认 1，可设为 1–100；
等于池子按耗尽处理。多击只表示同一技能每次都命中且期间无恢复；DoT、真实 Boss 连招、
不同技能组合、转化、暴击、格挡/闪避/法压、守护技能、Ward、MoM、恢复以及
Boss 阶段与打法都不会被偷偷算进结论。

**Boss 防御门槛规划** —— `POST /boss/threshold`。省略 `target_pool` 时，按当前抗性、
护甲和承受倍率回答低掷骰与完整掷骰区间分别至少需要多少整数防御池；提供目标池后，
还会返回池子缺口，并在其余输入不变时反解穿透前抗性或物理护甲。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/boss/threshold \
  -H 'Content-Type: application/json' \
  -d '{"skill":"Shaper Ball","level":84,"hit_count":3,"cold_resist_pct":75}'
```

抗性只在 -200%–90% 内搜索；物理护甲也受 PoB 物理减伤上限约束。上限内无法让累计承伤
严格低于目标池时返回不可达，不生成一个看似精确但游戏内无法达到的数值。该接口与生存
校验使用相同的“同一技能每次都命中、期间无恢复”边界。

**Boss 显式混合序列** —— `POST /boss/sequence`。调用方按顺序提交技能与可选重复次数，
系统逐击应用对应伤害类型的抗性/穿透或护甲，返回每一步的承伤区间、累计承伤、最坏余量、
按伤害类型的原始/承伤合计、最大单击承伤、最危险一击，以及最早可能耗尽与最迟必然耗尽的位置。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/boss/sequence \
  -H 'Content-Type: application/json' \
  -d '{"sequence":[{"skill":"Shaper Ball","count":2},{"skill":"Shaper Slam"}],"level":84,"pool":25000,"cold_resist_pct":75,"armour":10000}'
```

序列必须由调用方明确给出，展开后最多 100 击；任一步缺对应抗性、属于持续伤害、多伤害
类型或非零暴击时，整条序列返回 `unknown`。系统不会从 Boss 名称或经验猜测真实连招。

**Boss 定时命中与恢复时间轴** —— `POST /boss/timed-sequence`。每次事件必须给技能名与
绝对时间；系统先复用 `/boss/sequence` 的 Boss 伤害、穿透、抗性/护甲链得到逐击承伤，
再分别推进低掷骰与高掷骰两条防御池轨迹。持续恢复只发生在相邻命中之间，且可用
`recovery_delay_seconds` 表示每次受击后需要等待多久才重新开始恢复。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/boss/timed-sequence \
  -H 'Content-Type: application/json' \
  -d '{
    "events":[
      {"skill":"Shaper Ball","at_seconds":0},
      {"skill":"Shaper Ball","at_seconds":1.5},
      {"skill":"Shaper Slam","at_seconds":4}
    ],
    "level":84,
    "pool":25000,
    "cold_resist_pct":75,
    "armour":10000,
    "recovery_per_second":2200,
    "recovery_delay_seconds":0.5
  }'
```

结果逐击返回经过延迟后的有效恢复窗口、低/高掷骰路径实际恢复量、受击前与受击后池子、
最早可能/必然耗尽位置和全程最低余量。它还反解当前恢复下覆盖部分/完整伤害区间所需的
最小整数初始池，以及当前池子覆盖相同目标所需的恢复速率。因为“池子恰好降到 0”仍算
耗尽，连续恢复门槛以严格大于号披露；单击致死或同一时间的命中组致死时返回不可达，不会
生成无限大的伪精确恢复数值。

`POST /boss/timed-sequence/compare` 可比较 2–10 套完整时间轴/防御/恢复配置，分别列出存活
档位、最坏末尾池、全程最坏最低池、完整区间最低初始池与恢复门槛，并给出各口径最佳方案。
每套方案仍独立解析 Boss 伤害和防御，比较结果不扩写成药剂、装备或打法建议。

这是一个**显式假设计算器**：共享池上的固定恢复不能冒充生命再生、能量护盾回充、偷取、
药剂与击中回复的完整组合；也不含格挡、闪避、法压概率、Ward、MoM、DoT 或阶段机制。
事件顺序和时间完全来自调用方，系统不会声称它是游戏里的真实 Boss 连招。

**Boss 概率防御与生存分布** —— `POST /boss/probabilistic-sequence`。每个事件除技能与绝对
时间外，还必须明确标注 `hit_type=attack|spell`；系统不会从技能名猜攻击或法术。攻击事件
依次分为未命中、格挡、完整承伤，法术事件依次分为格挡、压制、完整承伤；格挡与压制是
互斥分支，不把两个概率乘成额外固定减伤。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/boss/probabilistic-sequence \
  -H 'Content-Type: application/json' \
  -d '{
    "events":[
      {"skill":"Shaper Ball","at_seconds":0,"hit_type":"spell"},
      {"skill":"Shaper Ball","at_seconds":1.5,"hit_type":"spell"},
      {"skill":"Shaper Slam","at_seconds":4,"hit_type":"attack"}
    ],
    "level":84,
    "pool":25000,
    "cold_resist_pct":75,
    "armour":10000,
    "recovery_per_second":2200,
    "spell_block_chance_pct":30,
    "spell_suppression_chance_pct":80,
    "attack_hit_chance_pct":75,
    "attack_block_chance_pct":40
  }'
```

结果不为伤害区间编造分布，而是分别返回 `low_roll` 与 `high_roll` 两条离散动态规划：每击
存活率、当击/累计耗尽率、期望末尾池、存活条件期望、P50/P10/P05 末尾池、P50/P90/P95
池损失，以及有界的最终池分布。默认桶宽为 `max(pool/2000, 0.01)`，响应同时披露桶数和
累计量化误差上界；也可显式调大 `bucket_size`。此外始终单列“每击都命中、未格挡、未压制、
全为高伤”的确定性最坏轨迹，防止高期望值被误读成保证。

`blocked_damage_taken_pct` 默认 0；`suppressed_damage_taken_pct` 默认 60，即当前基础法术压制
阻挡 40% 伤害。特殊格挡/压制效果必须由调用方覆盖。模型把各事件视为独立，不模拟游戏的
熵式闪避序列、幸运/不幸、不可闪避/不可格挡旗标，也不会在“该分支没有实际受击”时重排
恢复延迟；这些限制与事件不是实际 Boss 连招的边界都会原样返回。

`POST /boss/probabilistic-sequence/compare` 比较 2–10 套完整输入，分别给出最高高伤轨存活率、
最高 P05 末尾池、最高期望末尾池和确定性最坏仍能存活的方案。四个口径独立展示，不用期望
值替代尾部风险或确定性保证。

**Boss 分层防御池时间轴** —— `POST /boss/layered-sequence`。它不再把生命、能量护盾和
魔力合成一个共享数值。每次事件可用 `energy_shield_bypass_pct` 明确该击最终承伤中绕过
护盾的比例；未绕过部分先由护盾吸收，溢出与绕过部分再按全局 `mana_before_life_pct` 由
魔力承担，魔力不足的目标份额回落到生命。生命降到 0 即耗尽，护盾或魔力降到 0 本身不算死亡。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/boss/layered-sequence \
  -H 'Content-Type: application/json' \
  -d '{
    "events":[
      {"skill":"Shaper Ball","at_seconds":0,"energy_shield_bypass_pct":0},
      {"skill":"Shaper Ball","at_seconds":3,"energy_shield_bypass_pct":25}
    ],
    "level":84,
    "life":6000,
    "energy_shield":2500,
    "mana":1200,
    "mana_before_life_pct":30,
    "cold_resist_pct":75,
    "life_recovery_per_second":300,
    "energy_shield_recovery_per_second":100,
    "mana_recovery_per_second":180,
    "energy_shield_recharge_per_second":825,
    "energy_shield_recharge_delay_seconds":2,
    "energy_shield_recharge_interrupt_mode":"energy_shield_damage"
  }'
```

三个固定回复分别按自己的最大池封顶。护盾固定回复与护盾回充是两条独立路径：回充只有在
上次中断加延迟之后的时间窗生效；`energy_shield_damage` 只在该击实际由护盾吸收时中断，
`any_hit` 则让每个显式事件中断。结果为低/高伤害轨逐击列出回复前池、实际回复、回充窗口、
受击前池、护盾吸收/绕过、魔力目标/实付/短缺回落、生命承伤和受击后各池。

汇总同时给出各池实际吸收与回复、末尾/最低生命、回充中断次数和耗尽击，并在其它层保持
不变时分别反解最低生命、能量护盾与魔力。单轴门槛不能相加；如果所有伤害都绕过护盾，
单独提高护盾会明确返回 `unreachable`。`POST /boss/layered-sequence/compare` 可比较 2–10 套
完整分层方案，分别排序存活结论、高伤生命余量、高伤生命承伤和最低可达初始生命。

工具不会因为伤害是混沌就自动填护盾绕过，也不会从职业或装备猜 MoM、CI、EB、Ward；这些
都可能改变分池顺序。该分层工具本身不接偷取实例、击中回复和药剂；需要显式主动恢复事件时
使用下一节的恢复时间轴。Ward、守护技能和阶段机制仍保持在边界外。

**Boss 主动恢复事件时间轴** —— `POST /boss/recovery-sequence`。它在上述分层池和伤害路由
之上，接收三类显式恢复事件：

- `instant`：已经换算成池数值的瞬回，可指定 `before_hit` 或 `after_hit`；
- `timed_rate`：明确开始时间、每秒速率和持续时间的药剂式或其它持续恢复；
- `leech`：明确速率与持续时间的偷取实例。多个实例并行时先合计，再按生命 20%、能量护盾
  10%、魔力 20% 最大池每秒的基础上限裁剪；三项上限百分比都可显式覆盖。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/boss/recovery-sequence \
  -H 'Content-Type: application/json' \
  -d '{
    "events":[
      {"skill":"Shaper Ball","at_seconds":0},
      {"skill":"Shaper Ball","at_seconds":3,"energy_shield_bypass_pct":25}
    ],
    "recovery_events":[
      {"name":"生命药剂瞬回","at_seconds":1,"pool":"life","kind":"instant","amount":1200},
      {"name":"生命药剂持续段","at_seconds":1,"pool":"life","kind":"timed_rate","rate_per_second":400,"duration_seconds":2},
      {"name":"生命偷取实例","at_seconds":0,"pool":"life","kind":"leech","rate_per_second":600,"duration_seconds":3}
    ],
    "level":84,"life":5000,"energy_shield":1000,"mana":800,
    "mana_before_life_pct":30,"cold_resist_pct":75
  }'
```

同一时间点固定按“命中前瞬回 → 请求顺序中的全部同刻命中 → 命中后瞬回”结算。生命在命中
阶段恰好或低于 0 后，命中后与未来恢复都不能复活。每个池逐段披露固定回复、定时恢复、偷取
请求量/限速后量、护盾回充、实际生效与满池溢出；汇总另列偷取因速率上限被抑制的量。
同一积分段多个连续来源共同触及池上限时，各来源的“实际生效”按潜在恢复量等比例归属；总生效
与总溢出是精确值，来源归属只用于审计，不表示游戏内存在恢复优先级。

结果同时重跑一条“移除主动恢复、保留固定回复和护盾回充”的高伤基线，说明主动恢复带来的
末尾生命差以及是否真的改变存活结论。在其它池和恢复事件固定时仍分别反解生命、护盾与魔力
门槛。`POST /boss/recovery-sequence/compare` 比较 2–10 套完整方案，独立排列存活、高伤生命
余量、实际主动恢复量和最低初始生命；恢复总量最高不等于生存最好。

系统不会从攻击频率、输出伤害、装备词缀或药剂栏生成事件，也不推断充能、自动喝药、偷取
生成/移除、过量偷取保留、击回触发条件或恢复阻止。用户时间轴仍是假设，不是真实 Boss 连招。

**Boss 命中风险与防御缺口审计** —— `POST /boss/audit`。它横向检查本地当前收录的全部
Boss 技能预设，按指定 `hit_count`（1–100，默认 1）的最坏累计承伤排序；同时返回总预设数、
可评估数、未知数、三态计数、最危险已知项与每条未知原因。每个可评估项还会返回覆盖完整
掷骰区间所需的最小整数防御池、当前池子缺口，以及在其它输入不变时只补对应抗性或护甲的
目标与可达状态。顶层汇总所有已评估项的统一池子门槛、瓶颈技能、缺口技能数，以及单轴
补强可达/不可达计数。缺少某一类抗性或护甲时，其它已知项仍可规划，但顶层状态为
`partial`、`verdict=unknown`，未知项绝不会被排到末尾冒充低风险。

网页页脚“Boss 命中风险”入口把此审计与 `POST /boss/threshold` 接起来。输入的防御池必须
显式填写；抗性和护甲留空表示未知，审计会保留对应未知项。物理技能在护甲未知时不能从
网页打开单技能门槛，避免接口默认护甲 0 被误认为角色实测值。点击可评估技能后，详情会
显示当前池子与只补对应抗性或护甲的目标；修改输入、关闭弹窗或切换 API Key 都会清除
旧结果。网页不会自动读取 PoB Build，也不会把已评估项的统一门槛称作所有技能的结论。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/boss/audit \
  -H 'Content-Type: application/json' \
  -d '{"level":84,"pool":10000,"hit_count":2,"fire_resist_pct":75,"cold_resist_pct":75,"lightning_resist_pct":75,"chaos_resist_pct":0,"armour":10000}'
```

连续次数只表示“同一技能每次都命中且期间无恢复”的用户假设，不会从游戏机制自动推断多段
次数。这只是已收录 PoB 技能的累计承伤与防御缺口审计，不是完整技能清单，也不按出现频率、
命中概率、阶段、真实连招、恢复或打法加权，因此不能解释成 Boss 难度或击杀风险排行榜。

**Wiki Boss 建议与 PoB 快照对照** —— `POST /boss/guides/{boss_key}/build-check`
接受 PoB 分享码与可选 `same_boss_configuration`。只读取已抓取攻略的 `Recommendations`
章节里完整匹配的门槛原句，每项保留原文、页面 URL、抓取时间与内容哈希。当前库中
Sirus、Shaper、Elder 和 Uber Elder 有可结构化的 DPS、生命加护盾与三元素抗性建议；
其余页面保持 `unknown`。PoB 缺任一组成字段时不补零。DPS 只有用户确认 PoB 当前
敌人、技能和增益配置适用于所问 Boss，并且 PoB 保存了 Boss 档位，才显示数值相对
Wiki 门槛的差额。即使显示 `at_or_above`，它也只表示这两个数的关系；回复能力、药剂、
机制执行和页面建议的当前版本适用性仍列为缺口。

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

如果问题同时给出一个逐字 Metamod guard，可在同一请求增加
`"metamod_guard":"前缀无法被变更"`。响应会把基础通货的 POEDB 状态转移与
Craft of Exile 矩阵的 `YES / NO / N/A / YES & NO` 原值作为两条独立证据返回；
`operation_verdict` 保留基础状态结论，`metamod_interaction` 披露矩阵键、原值、原始注释
和未知版本范围。矩阵空白或没有记录时总 `verdict` 不会冒充完整结论。矩阵值不用于改写
状态转移，也不证明当前版本可执行。

聊天页的“基础通货状态”入口提供同一核对：可手工填写稀有度、前后缀数量与特殊状态，
也可粘贴完整装备复制文本；两种状态输入互斥。留空的稀有度和数量保持未知，明确填写的
`0` 才代表零条词缀。装备粘贴卡片上的“核对通货操作”只预填文本，选择通货并点击核对后
才会请求 API。页面分开展示 POEDB 效果、输入状态、条件后状态、逐项检查、仍未知项，
以及可选的 Metamod 矩阵原值。`compatible` 只表示已提供状态通过已知约束；矩阵空白
或缺失时，基础结论若为兼容，整体结论转为 `unknown`。输入变化、关窗或更换 API Key
后旧响应不能回写。

**独立尝试分布与材料预算** —— `POST /craft/attempt-plan`。这个端点不是通用 Craft
Planner；它只在 `affix_pool_odds` 已证明完整的胸甲普通同侧池上套用几何分布，并要求
调用方显式声明每次材料与停止上限。`include_market_prices=true` 时，只有整张清单都取得同
联盟、同来源、五分钟内的唯一代表价才输出成本：

网页页脚的“独立尝试预算”入口提供同一模型。每次材料由用户逐项填写，可明确选择是否
请求市场计价；打开面板或从歧义目标组中点选候选都不会自动发出计算请求。若词缀池不完整、
过期或目标不唯一，网页只展示缺口与候选组，不输出 0% 或材料预算。P50/P90/P95 是达到
相应累计概率所需的最少尝试数，超出设定上限的材料量与成本会标“超出上限”。任一材料
缺价时仍可显示数量预算，但不会拼接部分市场总价。输入变化、关窗或切换 API Key 后旧
响应不能回写。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/craft/attempt-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "base_type":"铁制背心","item_level":50,"generation_side":"prefix",
    "target_query":"IncreasedLife","max_attempts":100,"include_market_prices":true,
    "materials":[{"query":"混沌石","quantity_per_attempt":1,"category":"currency"}]
  }'
```

响应同时区分无限重试期望、在上限内提前成功停止的期望、P50/P90/P95 预算和最坏上限；
不会把任一项改写成混沌石等真实工艺的一次成功率，也不会补制作步骤。

**刷新胸甲普通词缀概率池** —— 概率和独立尝试计划要求 POEDB 来源快照在七天内。
运行环境可用下面三个命令检查、预检和发布；`preview` 与 `apply` 均重新抓取七张
胸甲属性词缀页，遵守项目抓取器的 robots 与限速配置：

```bash
python scripts/refresh_craft_pool.py status
python scripts/refresh_craft_pool.py preview
python scripts/refresh_craft_pool.py apply
```

`preview` 展示 `normal` 行增删、正权重前后缀数量及旧内部 ID 的延续情况，不改数据库。
`apply` 先用 SQLite Backup API 创建一致性备份，再把七页的 `normal` 行、来源审计和
词缀检索卡片作为一个事务发布。任何页面身份、完整行账、摘要或发布后来源证明失败，
整批回滚；预检后有人改动词缀页也会拒绝发布。`status` 可逐页看到是否仍可用于精确概率。

POEDB 当前把 `hover` 内部 ID 换成了哈希。刷新只对旧库里能按生成侧、词缀组、物等、
T 级、权重、名称和标签**唯一对账**的 `normal` 行沿用原 ID；新词缀使用来源的不透明 ID，
歧义对账则拒绝发布。精华、影响、地渊等其它类别继续保留旧行，不会因本次概率池刷新
失去既有 PoB 交叉来源关联。来源证明明确标记为 `normal` 范围，不把它说成其它类别也已刷新。

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

聊天页“精华状态”入口先从 `GET /craft/essences` 读取当前入库的精确名称、等阶和
物等上限；可按中英文名或 `T7` 等阶筛选，并由用户明确选择一件精华。手工模式允许
稀有度、物品等级或类别留空以保持未知；完整装备文本模式只发送精华名称和文本，
不会混入手工状态。装备粘贴卡片上的“核对精华操作”只预填文本，不自动提交核对。
结果分列来源效果、已知约束、条件后状态、类别保底词缀与仍未知项；来源效果原文漂移时
不会把旧语义或保底词缀当成操作结果。输入变化、关窗或切换 API Key 后旧响应不能回写。

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

聊天页“古灵通货状态”入口从 `GET /craft/eldritch/operations` 读取当前入库的已审计
操作及逐字来源状态。手工模式按操作只展示需要的部位、首要方、前后缀计数或双基底
词缀状态；空白保持未知，`0` 与明确的“没有”分别是已知输入。完整装备文本模式只
发送操作与文本；解析器不能从装备文本确定哪一方首要，因此三种首要方操作在该模式下
会保持未知。装备粘贴卡片只预填，不自动核对。页面分开展示 POEDB 条件状态与
Craft of Exile 矩阵原值；矩阵 `YES/NO` 不改变 POEDB 结论。来源效果原文漂移时
状态机与网页都不沿用旧的条件后结果。输入变化、关窗或切换 API Key 后旧响应不能回写。

**觉醒者之石双物品状态转移** —— `POST /craft/awakener`。可传两段游戏复制装备文本，
也可显式给出供体/目标的物品类别与传统影响。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/craft/awakener \
  -H 'Content-Type: application/json' \
  -d '{"donor_item_class":"项链","target_item_class":"项链","donor_influences":["塑界者"],"target_influences":["猎人"]}'
```

该例只确定供体会被摧毁，目标重铸为稀有并兼有塑界者与狩猎者影响。POEDB 效果原文
没有说明会保留哪条具体影响词缀，因此接口不会输出“各保留一条”、具体新词缀、概率或路线。

**加密通货状态转移** —— `POST /craft/veiled`。覆盖加密崇高石与加密混沌石；
指定底材类别时，同时返回本库 `category='veiled'` 的去重候选目录与前后缀分布。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/craft/veiled \
  -H 'Content-Type: application/json' \
  -d '{"operation":"加密崇高石","rarity":"稀有","item_class":"胸甲","prefix_count":2,"suffix_count":2}'
```

该例只确定随机移除一个词缀并添加一个随机加密词缀，同时枚举可能的前后缀计数。
候选目录不等于一次揭露实际展示的选项，页面生成权重也不会被换算成操作结果概率。
聊天页“加密通货状态”入口从 `GET /craft/veiled/operations` 读取当前入库的两种
精确操作，并标记来源效果是否仍与已审计原文逐字一致。手工模式可填写稀有度、
底材类别、前后缀数量与特殊状态；装备文本模式只传操作名和完整复制文本。
粘贴卡片只预填，不会自动执行。结果分开展示三态结论、效果来源、条件后状态、
逐项检查、未知项、隐匿来源词缀候选目录及元词缀矩阵原值。目录按每页 20 条
翻页，不静默截断；来源行数、去重条数和前后缀分布均明确列出。页面的
`weight` 仅为来源页生成权重，不表示加密通货的结果概率。来源效果原文漂移时
后端与网页都不沿用旧状态转移，矩阵原值仍仅作独立并列披露。输入改变、关窗
或切换 API Key 后，旧清单与旧结果不会回写。

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
  - 该底材页最高档位是 T1（115-129），要求物品等级 54[[E1]]
  - 物等校验：当前物品等级已达到该档位门槛；这不表示某种工艺可执行
  - 互斥：+(41-45) 最大生命 / 每秒再生 4 魔力（工艺工作台 T1/6）[[E1]]

市场价值
**本报告不自动联网。** 如需可审计的稀有装备同类挂单比较，调用
`POST /economy/rare-comparables`；它也不等于该装备的精确成交价。
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
  同类别下不同底材页面给出不同档位、隐式词缀的 T 级来源尚未接入。
- **具体底材可收窄词缀页。** 已入库的 PoB 底材英文名与 POEDB 中文名逐字桥接；
  装备类别和底材相符，且 PoB 护甲属性标签能落到现有 POEDB 词缀页时，
  `item_class_pages` 收窄到该页，`page_resolution=base`，报告附双方来源引用。
  中文底材重名但所有记录指向同页时可定页面，不会据此冒认某一件底材；
  类别矛盾、Ward 等无对应词缀页时不给 T 级。英文固有隐式核对 PoB 原文；中文固有
  隐式核对 POEDB 同 slug 底材卡片，并要求整条数值序列与 PoB 同底材定义一致。
  原文和可变数值区间完整吻合时才报告区间与 roll；**隐式 T 级仍保持未匹配**。
- **药剂按底材子类定页。** PoB 的 Life/Mana/Utility 子类和标签须一致，
  再用 POEDB 的词缀页 `item_class_code` 核对。真银与坚铁药剂按逐字 slug
  进入各自专页；普通功能药剂进入 `Utility_Flasks`。已入库 48 件药剂中
  42 件有可核实页面，6 件复合药剂没有对应词缀页，保持未匹配。
- **可制作方向只说明已核实的档位、门槛与互斥关系。** `craft_directions`
  在结构化 API 中返回目标档位、逐页来源 URL、物等门槛状态和互斥词缀。
  多页面类别须逐页核实最高档位、区间与物等门槛完全一致；有缺页或分歧
  就不给单一目标。当前物等不足时明确标记 `unmet`；来源/词缀组歧义时
  不生成方向。达到物等门槛不等于已知可执行工艺或成功率。
- **中文普通复制的相邻多行只在整条可核实时合并。** 例如项链上的
  `+5% 所有元素抗性` 与紧随其后的 `召唤生物获得 +7% 元素抗性`，完整拼接后
  命中 POEDB 工艺台 T2/2，按**一条后缀**计算容量。系统检查两至三行的同段、
  同类型与旗标、完整模板、全部数值区间和来源/底材档位与方向歧义；各行都能独立成立，
  或候选分组重叠时，保留原行并让无法确定的部分显示「未匹配」。合并会写入
  `warnings`，原始复制文本不会被改写。进阶复制仍按游戏给的词缀头分组。
- **同类挂单不等于精确估值。** `economy_rare_comparables` 已能按精确底材、物等、
  腐化状态和成功映射的显式词缀查官方在线挂单，也能可选核对品质和连接组尺寸；
  仍不约束词缀 roll、插槽颜色或位置，也没有成交记录。因此「是否值得裂界 / 镜像湖 / 够不够镜子级别」三项仍一律
  输出「数据不足，不作判断」；报告只把有据事实交给用户判断。

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

# 按用户明确目标生成成长里程碑；priority 数字越小越优先
curl -s -X POST http://127.0.0.1:8787/poe_agent/build \
  -H 'Content-Type: application/json' \
  -d '{
    "code": "https://pobb.in/你的分享ID",
    "growth_plan": {
      "objectives": [
        {"metric": "fire_resistance", "target": 75, "priority": 1},
        {"metric": "life", "target": 5000, "priority": 2},
        {"metric": "dps", "target": 1000000, "priority": 3, "label": "Boss 输出"}
      ]
    }
  }'

# PoB 导入后同时校验一记 Boss 命中；防御池默认取未保留生命 + 能量护盾
curl -s -X POST http://127.0.0.1:8787/poe_agent/build \
  -H 'Content-Type: application/json' \
  -d '{
    "code": "https://pobb.in/你的分享ID",
    "boss_survival": {
      "skill": "Shaper Ball",
      "level": 84,
      "pool_mode": "life_and_energy_shield"
    }
  }'

# PoB 导入后用同一份 PlayerStat 基础防御快照校验用户指定的混合序列
curl -s -X POST http://127.0.0.1:8787/poe_agent/build \
  -H 'Content-Type: application/json' \
  -d '{
    "code": "https://pobb.in/你的分享ID",
    "boss_sequence": {
      "sequence": [
        {"skill": "Shaper Ball", "count": 2},
        {"skill": "Shaper Slam"}
      ],
      "level": 84,
      "pool_mode": "life_and_energy_shield"
    }
  }'

# 用纯生命 PlayerStat 与净生命回复推进用户显式标时的命中时间轴
curl -s -X POST http://127.0.0.1:8787/poe_agent/build \
  -H 'Content-Type: application/json' \
  -d '{
    "code": "https://pobb.in/你的分享ID",
    "boss_timed_sequence": {
      "events": [
        {"skill": "Shaper Ball", "at_seconds": 0},
        {"skill": "Shaper Ball", "at_seconds": 2}
      ],
      "level": 84,
      "pool_mode": "life",
      "recovery_delay_seconds": 0
    }
  }'

# 横向审计这份 Build 面对当前收录 Boss 技能连续两次命中的防御缺口
curl -s -X POST http://127.0.0.1:8787/poe_agent/build \
  -H 'Content-Type: application/json' \
  -d '{
    "code": "https://pobb.in/你的分享ID",
    "boss_risk_audit": {
      "level": 84,
      "hit_count": 2,
      "pool_mode": "life_and_energy_shield"
    }
  }'

# 从 PlayerStat 读取法术格挡/压制，计算显式法术时间轴的生存分布
curl -s -X POST http://127.0.0.1:8787/poe_agent/build \
  -H 'Content-Type: application/json' \
  -d '{
    "code": "https://pobb.in/你的分享ID",
    "boss_probabilistic_sequence": {
      "events": [
        {"skill": "Shaper Ball", "at_seconds": 0, "hit_type": "spell"},
        {"skill": "Shaper Ball", "at_seconds": 2, "hit_type": "spell"}
      ],
      "level": 84,
      "pool_mode": "life"
    }
  }'

# 从 PlayerStat 分别读取生命、护盾、魔力与回复，推进显式分层时间轴
curl -s -X POST http://127.0.0.1:8787/poe_agent/build \
  -H 'Content-Type: application/json' \
  -d '{
    "code": "https://pobb.in/你的分享ID",
    "boss_layered_sequence": {
      "events": [
        {"skill": "Shaper Ball", "at_seconds": 0},
        {"skill": "Shaper Ball", "at_seconds": 3, "energy_shield_bypass_pct": 25}
      ],
      "level": 84,
      "mana_before_life_pct": 30
    }
  }'

# 从 PlayerStat 取分层池与固定回复，主动药剂/击回/偷取事件仍逐项显式提供
curl -s -X POST http://127.0.0.1:8787/poe_agent/build \
  -H 'Content-Type: application/json' \
  -d '{
    "code": "https://pobb.in/你的分享ID",
    "boss_recovery_sequence": {
      "events": [
        {"skill": "Shaper Ball", "at_seconds": 0},
        {"skill": "Shaper Ball", "at_seconds": 3}
      ],
      "recovery_events": [
        {"name": "生命药剂", "at_seconds": 1, "pool": "life", "kind": "instant", "amount": 1200},
        {"name": "生命偷取", "at_seconds": 0, "pool": "life", "kind": "leech", "rate_per_second": 600, "duration_seconds": 3}
      ],
      "level": 84
    }
  }'

# 把显式命中、DoT 暴露窗口与恢复放入同一分层时间轴
curl -s -X POST http://127.0.0.1:8787/poe_agent/build \
  -H 'Content-Type: application/json' \
  -d '{
    "code": "https://pobb.in/你的分享ID",
    "boss_dot_sequence": {
      "events": [
        {"skill": "Shaper Ball", "at_seconds": 0},
        {"skill": "Shaper Ball", "at_seconds": 4}
      ],
      "dot_windows": [
        {
          "name": "用户假设的燃烧地面暴露",
          "start_seconds": 0.5,
          "duration_seconds": 2.5,
          "damage_type": "Fire",
          "damage_basis": "raw",
          "dps_low": 3000,
          "dps_high": 3600,
          "energy_shield_bypass_pct": 0
        }
      ],
      "recovery_events": [
        {"name": "生命药剂", "at_seconds": 1, "pool": "life", "kind": "instant", "amount": 1200}
      ],
      "level": 84,
      "dot_recharge_interrupt_mode": "energy_shield_damage"
    }
  }'

# 在命中/DoT/恢复进入分层池前应用显式临时减伤乘数和 Guard 容量
curl -s -X POST http://127.0.0.1:8787/poe_agent/build \
  -H 'Content-Type: application/json' \
  -d '{
    "code": "https://pobb.in/你的分享ID",
    "boss_mitigation_sequence": {
      "events": [
        {"skill": "Shaper Ball", "at_seconds": 0},
        {"skill": "Shaper Ball", "at_seconds": 3}
      ],
      "dot_windows": [{
        "name": "用户假设的燃烧地面暴露",
        "start_seconds": 0,
        "duration_seconds": 3,
        "damage_type": "Fire",
        "damage_basis": "raw",
        "dps_low": 3000,
        "dps_high": 3600
      }],
      "recovery_events": [],
      "mitigation_windows": [{
        "name": "用户显式 Guard 窗口",
        "start_seconds": 0,
        "duration_seconds": 2.5,
        "scope": "both",
        "damage_types": ["Fire"],
        "taken_multiplier": 0.8,
        "guard_capacity": 1500,
        "priority": 10
      }],
      "level": 84
    }
  }'

# 在 Guard 后逐击推进 Ward ready/broken、破裂与恢复
curl -s -X POST http://127.0.0.1:8787/poe_agent/build \
  -H 'Content-Type: application/json' \
  -d '{
    "code": "https://pobb.in/你的分享ID",
    "boss_ward_sequence": {
      "events": [
        {"skill": "Shaper Ball", "at_seconds": 0},
        {"skill": "Shaper Ball", "at_seconds": 1},
        {"skill": "Shaper Ball", "at_seconds": 2}
      ],
      "dot_windows": [{
        "name": "用户假设的燃烧地面暴露",
        "start_seconds": 0,
        "duration_seconds": 3,
        "damage_type": "Fire",
        "damage_basis": "raw",
        "dps_low": 3000,
        "dps_high": 3600
      }],
      "recovery_events": [],
      "mitigation_windows": [],
      "ward": {
        "capacity": 1200,
        "restore_delay_seconds": 2,
        "initial_state": "ready",
        "break_mode": "break",
        "broken_hit_timer_mode": "preserve"
      },
      "level": 84
    }
  }'

# 以“当前”为基线比较两个 PoB 方案；可省略 metrics 以检查全部受支持指标
curl -s -X POST http://127.0.0.1:8787/poe_agent/build/compare \
  -H 'Content-Type: application/json' \
  -d '{
    "reference_label": "当前",
    "scenarios": [
      {"label": "当前", "code": "第一份 PoB 分享码"},
      {"label": "候选", "code": "第二份 PoB 分享码"}
    ],
    "metrics": ["life", "dps", "fire_max_hit"]
  }'
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
可选的 `growth_plan` 接受 1–20 个用户显式目标，覆盖可用生命、能量护盾、四抗、法压、
双格挡、护甲、闪避、生命回复、当前技能 DPS 与五类 Max Hit。响应按 `priority` 排序，
逐项目给出 `met / gap / unknown`、实际采用的 PlayerStat 字段、当前值、目标、精确差额、
下一里程碑和复核方式。未保留生命、有效法压/格挡和 DPS 都按既有字段优先级选择；缺当前
字段时不从装备、职业或其它指标估算。目标值完全来自本次请求——不会自动把长期记忆里的
自然语言目标变成数值，也不会声称目标一定可达；具体装备、天赋、工艺、机会成本与市场价
仍未规划。修改后必须在相同技能、敌人档位和配置下重算 PoB，才能比较是否达标。
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

可选的 `boss_survival` 会把 Boss 伤害模型与这份 Build 保存的防御快照接起来。若 Build 的
`presetBossSkills`、`enemyIsBoss`、有效敌人等级及显式穿透覆盖都与请求一致，并导出了对应
伤害类型的 `*MaximumHitTaken`，系统优先直接比较 PoB 完整 Max Hit；这个值保留 Build 当前
配置下的伤害转化、承伤修正、守护技能、Ward、MoM 等复杂防御，不再用局部字段近似。
PoB 在三种元素 Max Hit 相等时只导出 `LightningMaximumHitTaken`，该导出契约也被显式识别。
需要刻意查看基础生命/护盾口径时，可传 `use_pob_max_hit=false` 禁用优先路径。
`hit_count>1` 时也必定禁用这条路径，因为 `*MaximumHitTaken` 只是单击阈值，不能相加。

配置不匹配或未导出 Max Hit 时，结果会通过 `fallback_reason` 说明原因，再降级到基础模式：
默认 `pool_mode=life_and_energy_shield`，生命优先使用 `LifeUnreserved`；标准 PoB 导出在生命
未保留时不写该字段，此时才回退 `Life`。对应元素抗性和物理命中的护甲也只按原字段映射，
响应通过 `source_fields` 列出每一个实际采用的字段。也可选 `pool_mode=life`，只校验生命池。
缺少池、对应抗性或护甲时返回 `unknown`；混沌命中选择生命+护盾而 Build 有护盾时，因为
无法仅从这些字段判断 CI、Shavronne 等绕过规则，也返回 `unknown`。`PhysicalDamageReduction`
是 PoB 对某个伤害口径的面板估算，不会被冒充额外固定物理减伤。基础模式可累加同一命中，
但不覆盖真实连招、持续伤害、恢复、阶段与打法。Boss 技能和公式引用会随
`boss_survival.citations` 一起返回。

可选的 `boss_sequence` 把同一份 PlayerStat 基础字段接到用户明确给出的多技能序列：
池子仍按 `pool_mode` 取 `LifeUnreserved`/`Life` 与可选 `EnergyShield`，只读取序列实际出现
伤害类型所需的 `FireResist` / `ColdResist` / `LightningResist` / `ChaosResist`，物理命中
才读取 `Armour`。结果在逐击轨迹之外返回 `damage_type_totals`、`most_dangerous_*` 与
`largest_single_hit_taken_*`。混合或多击序列**始终不使用** `*MaximumHitTaken`，因为单击
阈值无法拆回各类逐击承伤；该原因会写入 `fallback_reason`。任一所需字段缺失，或混沌伤害
与能量护盾的绕过机制无法从快照确定时，整条序列返回 `unknown`，不输出局部生存结论。

可选的 `boss_timed_sequence` 在同一基础防御映射上增加显式 `at_seconds`。默认
`pool_mode=life`，并按 `NetLifeRegen` → `LifeRegenRecovery` → `LifeRegen` 的顺序只选择
一个已保存的生命回复字段；实际字段会与生命、抗性、护甲一起写入 `source_fields`。调用方
也可以显式提供 `recovery_per_second` 覆盖该选择，并通过 `source_config.recovery_source`
区分用户输入和 PlayerStat。选择 `life_and_energy_shield` 时，生命与护盾是两个分立池，系统
不会擅自把生命再生、护盾再生或护盾回充相加成共享恢复；除非调用方明确给出共享恢复速率，
否则返回 `unknown`。混合时间轴同样不使用 PoB 单击 `*MaximumHitTaken`，原因会保留在
`fallback_reason` 中。

可选的 `boss_probabilistic_sequence` 继续复用上述逐击伤害与恢复链，但要求每个事件显式标注
`attack` 或 `spell`。攻击事件默认从 `EvadeChance` / `EvasionChance` 读取玩家闪避率并转换为
敌方命中率，再读取 `EffectiveBlockChance` / `BlockChance`；法术事件读取
`EffectiveSpellBlockChance` / `SpellBlockChance` 与 `EffectiveSpellSuppressionChance` /
`SpellSuppressionChance`。只读取本序列实际涉及的类别，来源和 `100%-闪避率` 转换都会写入
`source_config`。任一相关字段缺失时整条分布返回 `unknown`，不会把缺失格挡或压制静默当成
0%；调用方可以用同名请求字段显式覆盖。概率结果仍同时返回无概率防御的确定性最坏轨迹，
并披露离散桶宽与误差；PoB 单击 `*MaximumHitTaken` 同样不会拆入多击分布。

可选的 `boss_layered_sequence` 从 PlayerStat 分别读取 `LifeUnreserved` / `Life`、
`EnergyShield` 和启用魔力承伤时的 `ManaUnreserved` / `Mana`。回复按池分别选择
`Net*Regen` → `*RegenRecovery` → `*Regen`，护盾回充单独读取 `EnergyShieldRecharge`；
实际采用字段与显式覆盖都写入 `source_fields` / `source_config`。能量护盾为 0 时不要求护盾
回复和回充，`mana_before_life_pct=0` 时不要求魔力与魔力回复；反之缺任一实际使用字段时整条
返回 `unknown`。每击的 `energy_shield_bypass_pct` 和全局魔力承伤比例始终由请求提供，系统
不会从职业、天赋、装备或伤害类型猜测。

可选的 `boss_recovery_sequence` 复用同一套分层池、净回复和护盾回充取值，再叠加请求中的
`recovery_events`。Build 不会把 `LifeLeechGainRate` 等快照自动变成恢复事件：这些字段按 PoB
口径可能混合 Leech 与 On Hit，且不包含事件发生时间。调用方必须明确瞬回阶段，或持续/偷取
事件的开始、速率和持续时间；返回的 `source_config` 会把池字段与显式事件来源分开披露。
主动恢复时间轴同样不拆用 PoB 单击 `*MaximumHitTaken`。

可选的 `boss_dot_sequence` 在上述分层池和主动恢复上增加 1–100 个显式 DoT 暴露窗口。
每个窗口必须给出唯一名称、开始时间、持续时间、伤害类型、低/高 DPS、`raw|taken` 口径和
护盾绕过比例。`raw` 口径只应用对应 PlayerStat 抗性、显式固定物理减伤与承伤倍率；物理
DoT 不使用护甲，任何 DoT 都不套命中穿透、压倒或自动 Uber 倍率。`taken` 口径视为已经
完成减伤，因而不要求对应抗性。系统按 DoT 开始/结束、命中、恢复窗口及池触边时点切分，
同时推进连续伤害与恢复，不靠固定采样步长；响应返回逐段池流量、各来源计划/耗尽前伤害、
精确首次耗尽时间、恢复抵消、移除 DoT 的高伤基线和三池单轴门槛。

DoT 对护盾回充的影响必须通过 `dot_recharge_interrupt_mode` 明确选择：护盾路径持续受损、
任意持续伤害，或不打断。这个字段是计算假设，不是对某个技能机制的推断。Build 入口只从
PlayerStat 读取基础池、抗性、净回复、护盾回充和命中链所需护甲；窗口、叠层、刷新、阶段、
地面覆盖与走位全部保持显式，不把用户窗口称为 Boss 的真实技能时间轴。

可选的 `boss_mitigation_sequence` 在上述混合时间轴进入护盾/魔力/生命之前应用 1–100 个
显式临时防御窗口。每个窗口给出 `[start, end)`、`hit|dot|both` 范围、可选伤害类型、
`taken_multiplier`、一次性 `guard_capacity` 和优先级。重叠窗口先按优先级、开始时间、名称
依次应用乘数，再由同序 Guard 吸收；前序 Guard 吸收后的残余才交给后序窗口。DoT 会在 Guard
精确耗尽时切段，因此低/高伤轨可以有不同耗尽时刻；命中恰好发生在窗口结束点时不受保护。

响应分别披露逐击和逐段的伤害前后值、各窗口乘数减免、Guard 吸收、未使用容量、耗尽时刻，
以及移除全部临时防御后的高伤基线。这里的 Guard 是显式数学容量，不自动等同于任何具体
守护技能或 Ward：系统不读取装备词缀生成窗口，不模拟施法动作、冷却、Buff 移除、Ward
破裂/复原或阶段循环。`POST /build.boss_mitigation_sequence` 仍只从 PlayerStat 提供基础池、
抗性、回复、回充和命中护甲，全部临时防御参数由请求逐项冻结。

可选的 `boss_ward_sequence` 再把命中残余交给独立 Ward 状态机。Ward 只吸收命中，不处理
DoT；每次请求必须显式给容量与恢复延迟，并选择初始 `ready|broken`。初始 broken 还要提供
绝对 `initial_restore_at_seconds`，避免把“恢复需要多久”和“当前还剩多久”混成一个字段。
`break` 模式的首个正残余命中吸收最多一个 Ward 容量后立即 broken，即使本击没有用完整容量
也不会把余额带到下一击；`unbreakable` 模式则每次命中重新使用完整容量。

broken 期间继续受击可显式选择 `preserve` 原恢复时点，或 `reset` 为本击时间加恢复延迟。
同刻命中按请求顺序结算，恢复延迟为 0 时可在下一次同刻命中前重新 ready。低/高伤轨各自
推进，因此前置 Guard 把低伤残余完全吸收、却给高伤留下残余时，两条 Ward 状态会分叉。
响应逐击披露命中前状态、是否刚恢复、吸收、破裂、计时重置和下次恢复点，同时给出移除
Ward 后的高伤基线。Build 入口不会读取 PoB `Ward` 面板值：静态容量无法证明当前 ready、
Buff 正在生效或恢复计时为何值，全部 Ward 状态字段仍由请求冻结。

可选的 `boss_risk_audit` 使用相同的基础字段，对本地全部 Boss 技能预设按 `hit_count`
逐条累计。它会返回已知项风险排序、`assessment_status=complete|partial|none`、覆盖计数、
三态计数、最危险已知项、逐技能池子/抗性/护甲缺口与统一池子门槛。跨技能审计不会复用
某一特定 PoB 配置的
`*MaximumHitTaken`。如果 Build 有能量护盾，混沌项会因绕过机制不明保持 unknown，
其它伤害类型仍可形成明确的部分排名；未知项不会被当成安全或低风险。

**Build 多方案比较**只接受 2–5 份完整 PoB 分享码或白名单托管链接，不接受只含天赋节点、
没有 PlayerStat 的官方树链接。每个候选都与显式基线逐项比较；只有 Calcs 当前技能、完整
PoB 配置和当前激活树版本一致，并且双方实际采用同一个 PlayerStat 字段时，才输出绝对与
百分比变化。一个候选不可比不会抹掉其它候选的有效差值，指标会分别标成 `ready`、
`partial`、`unknown`。响应同时列出装备槽位、启用宝石（含可用的等级与品质）与天赋节点的
结构变化，但只陈述变化，不把它们冒充数值变化的因果来源。不同指标单位和取舍不可直接相加，因此固定返回
`overall_verdict=not_ranked`，不生成总体分数、“最佳方案”、装备推荐或市场价格结论。

**PoB 阶段路线** —— `POST /planner/league-starter` 接受当前 PoB 和 1–5 份按顺序排列的
阶段 PoB，每阶段必须由用户填写名称与目标。返回目标等级、职业、当前计算技能、变动槽的目标装备名称与底材、
宝石及天赋节点的相邻阶段变化，以及逐指标的目标快照值。仅当两阶段的当前计算技能、
完整配置、树版本和所用 PlayerStat 字段一致时，才计算数值差额；否则标记 `unknown`
并列出原因。每阶段给出重新导出完整 PoB 的复核方式。网页底部“PoB 阶段路线”可直接操作。
分享码解压后的 XML 限制为 5 MB，防止高压缩率输入耗尽内存。路线完全来自用户提交的
快照，不代表自动最优配装、游戏门槛、经济收益或结构变化对数值的因果解释。

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/planner/league-starter \
  -H 'Content-Type: application/json' \
  -d '{
    "current_code": "当前 PoB 分享码或白名单链接",
    "stages": [
      {"label": "进入白图", "goal": "核对自己的白图阶段目标", "code": "阶段 PoB 分享码或链接"},
      {"label": "进入红图", "goal": "核对自己的红图阶段目标", "code": "下一阶段 PoB 分享码或链接"}
    ],
    "metrics": ["life", "dps", "fire_resistance"]
  }'
```

**截图识别** —— 聊天页点击“上传截图”，支持不超过 5 MB 的 PNG、JPEG 与 WebP。
`llm.vision_enabled` 默认开启，使用与聊天相同的 DeepSeek 模型、网关和凭证；
关闭时 `POST /screenshot/recognize` 明确返回 503。图片只在该次请求内转给模型，
不写磁盘、不进数据库。装备截图先显示可编辑原文，用户确认后进入 `/item`；Build
截图只保留白名单内可见字段，确认后进入 `/build/screenshot`，缺失项保持未知。
已用合成数字图片验证目标模型的视觉输入；这不等于真实游戏截图识别准确率基准，
模糊、裁切与同名物品仍需用户复核。

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

单实体的“是什么 / 有哪些词缀”问题在精确别名只命中一个实体时，仅把该实体的已编号
POEDB 证据交给模型，并关闭后续工具 schema；多实体与比较问题仍走完整检索和工具路径。
2026-10-30 用本机已配置网关实测“猎首（Headhunter）有哪些词缀？”：非流式
`723` token、单条 POEDB 引用、0 条拦截；优化前同问约 `15,640` token，
且可能把仿品一并带入。`scripts/verify_chat.py` 对这些边界有在线断言。

自然语言描述也能命中，走的是词缀意图通道：

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "偷取词缀的腰带", "top_k": 3}'
```

可选参数：`top_k`（1–50）、`category`（按一级分类过滤）。

**查询跨来源冲突与待核对差异** —— 网页页脚的“跨来源核对”入口可查看台账概览，按分类、
处理状态、kind、来源和字面内容筛选，逐页打开两侧原值与来源标识。普通用户只读接口与
聊天/MCP 工具共用同一套分类和过滤实现。网页与 API 默认值都是 `status=confirmed`、
`resolution=active`；只有明确选择或传入
`status=review` 或 `all` 才会看到 `kind` 以 `_review` 结尾的待核对记录：

```bash
curl -s -X POST http://127.0.0.1:8787/poe_agent/conflicts/lookup \
  -H "Authorization: Bearer $POE_AGENT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"status":"confirmed","resolution":"active","limit":20}'

curl -s 'http://127.0.0.1:8787/poe_agent/conflicts/summary' \
  -H "Authorization: Bearer $POE_AGENT_API_KEY"

curl -s 'http://127.0.0.1:8787/poe_agent/conflicts/42' \
  -H "Authorization: Bearer $POE_AGENT_API_KEY"
```

逐条查询还支持 `query`（对象、字段或两侧原值的字面搜索）、`kinds`、`sources`、
`resolution=active|resolved|all` 与 `cursor`。`query` 中的 `%`、`_` 按普通字符处理，
不会变成 SQL 通配符。响应同时返回本页 confirmed/review/resolved 计数、总匹配数、
`has_more` 和 `next_cursor`；后续页必须保持完全相同的过滤条件，改变任一条件都会得到 422，
避免把不同查询的页面拼成一份清单。

`GET /conflicts/{id}` 按正整数 ID 读取单条记录，找不到返回 404；详情包含两侧来源标识、
原值、原始长度、截断标记、审计状态和 `can_support_facts`。单侧原值最多显示前 16000 字，
超出部分不会传给网页。只有未解决的 confirmed 记录具有事实资格；review 和已解决历史
记录均为 `false`。网页仅把可信 HTTPS 来源域名渲染为外链，其他来源地址保留在 API 数据中。

`GET /conflicts/summary` 分开报告 confirmed/review、active/resolved、kind、来源对和 review
成因。**台账总数不等于已确认冲突数**：当前 `spectre` 8 条属于 confirmed；
`mod_review`、`unique_review`、`spectre_review`、`gem_review`、`tree_review` 和
`essence_review` 合计 539 条都只是待核对差异。查询只复述双方原值与审计状态，不裁决
哪一侧正确；已确认项的并列块仍按来源优先级推荐 POEDB，review 不获得这个出口。

**健康检查** —— 反映 SQLite、向量扩展、已注册工具、LLM 凭证池状态。

```bash
curl -s http://127.0.0.1:8787/poe_agent/healthz
```

```
status = ok
db     = entities 1341 / chunks 25759 / aliases 2691, journal_mode=wal
vector = extension_available true, requested false, enabled false, reason="配置关闭"
llm    = deepseek-v4-1-flash-1017, credentials 1
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

注意 `/admin/conflicts` 与普通查询共用同一条分类边界：任意 `kind` 只要以 `_review`
结尾，就只是「两源对不上，待人工或确定性规则核对」，不得当作冲突呈现；其它 `kind`
才属于 confirmed。这个后缀规则不因两侧原值看起来不同而改变。

### 检索结果里的 `degraded=true` 是什么意思

配置主动关闭时不是故障。单 DeepSeek 模式不启用远程 Embedding / ReRank，
`retrieval.vector.enabled` 和 `retrieval.rerank.enabled` 保持 `false`。
误设为 `true` 时，运行时仍阻止任何远程向量/重排调用，并在 `/healthz` 披露策略原因；
正常检索继续使用 BM25、实体精确与关键词，引用与充分依据闸门保持不变。
旧索引数据保留，`scripts/backfill_vectors.py` 只读检查其维度、数量和失效标记；远程适配器及向量查询/回填算法已移除。
整轮检索超时仍保留已完成候选，对没有实体精确命中的候选关闭充分依据闸门。

`POST /search` 及 `kb_search` 的 `category`、`league`、`version` 精确过滤同时作用于
BM25、实体精确和词缀意图候选；没有对应元数据的切片不会混入指定版本的结果。

---

## 数据源与优先级

优先级内建于检索排序，不是提示词里的建议。

| 级别 | 来源 | 说明 |
| --- | --- | --- |
| P1 | POEDB | 中文名与页面说明的真源，最高优先级；冲突时推荐采用 |
| P1 | GGG Trade API | 官方实时在线挂单备用源；仅报过滤后的报价中位数，不代表成交记录 |
| P2 | Path of Building `src/Data/` | 数值与结构的真源（GGG 客户端 `.dat` 机器导出），无中文名 |
| P3 | PoE Wiki | 已接入八篇终局 Boss 攻略章节；其它 Wiki 领域未接入 |
| P4 | Craft of Exile | 尚未接入 |
| P5 | poe.watch | 已接入实时价格快照；统计值不是保证成交价 |
| P6 | GGG 官方 Patch Notes | 已接入白名单论坛首帖核验、版本与日期、逐行原文查询；历史覆盖取决于导入页数 |
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
| `scripts/db_backup.py` | WAL 安全的一致性快照、清单列表、深度校验与显式离线恢复 |
| `scripts/ci_smoke.py` | 无密钥/无真实库的全新 SQLite → 备份 → 恢复 → OpenAPI 发布冒烟 |
| `scripts/migration_smoke.py` | CI 中 Linux 生成带中文 FTS 的 SQLite 与配置件，Windows 全新安装后复制并启动真实进程验收 |
| `scripts/import_ggg_patch_notes.py` | GGG 官方 PoE1 公告预检与原子入库，默认不写数据库 |
| `scripts/import_boss_guides.py` | 八篇 PoE Wiki 终局 Boss 攻略章节预检与原子入库，默认不写数据库 |
| `scripts/backfill_base_implicits.py preview/apply` | 离线核对现有底材与 POEDB 中文固有隐式；apply 先备份再回填，译名漂移的底材跳过 |
| `scripts/test_search.py` | 检索链路验证 |
| `scripts/verify_mods.py` | POEDB 词缀自检：模板往返命中率、T 级不变量 |
| `scripts/verify_uniques.py` | 唯一装备抽取抽样核对 |
| `scripts/probe_gateway.py` | LLM 网关能力探测（模型清单 / tool calling / 流式 / 推理特征） |
| `scripts/verify_llm.py` | LLM 客户端验证（`--offline` 无需凭证） |
| `scripts/probe_pob.py` | PoB 数据资产清点与解析可行性验证 |
| `scripts/import_pob_mods.py` | 导入 PoB 词缀（`--dry-run` 只体检） |
| `scripts/import_pob_constants.py` | 导入游戏常量（`calc/` 真源） |
| `scripts/import_pob_uniques.py` | 原子导入 PoB 当前唯一装备与隔离的命名变体历史 |
| `scripts/verify_pob_mods.py` | POEDB × PoB 词缀对账 |
| `scripts/verify_pob_uniques.py` | POEDB × PoB 唯一装备对账 |
| `scripts/verify_chat.py` | `/chat`、`/chat/stream`、`/item` 端到端验证，61 项（`--gap-only` 31 项，不需要凭证） |
| `scripts/check_spec_refs.py` | 校验 spec 里的需求交叉引用 |
| `scripts/check_doc_facts.py` | 核对文档里的数字断言（单测项数、库内行数）与实际状态是否一致 |
| `scripts/check_portability.py` | AST 平台中立性检查：平台专有模块、shell 依赖、路径 API 与文本编码 |

| `scripts/eval_run.py` | 黄金问答集评测：检索侧指标 / 录制回放 / 出口闸门指标 / 人工审计清单 |
| `scripts/import_pob_bases.py` | 导入 PoB 底材注册表，18 个装备类别（默认不联网；`--dry-run` 清点、`--category` 单类、`--fetch` 缺页补抓） |

### 评测：四项指标与黄金问答集

`config.yaml` 里的阈值与 `orchestrator/binding.py` 的闸门规则都需要判据，否则改动之后
无法判断是变好还是变坏。判据来自 `tests/golden/questions.yaml`（198 条，六类样本：
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

目前 2502 项单元测试覆盖以下与「无据不答」直接相关的重点：

| 组 | 项数 | 重点 |
| --- | --- | --- |
| 出口绑定闸门 | 47 | 事实数值溯源、计算器派生回填与成对的错误拒答/无据漏出压制 |
| 装备文本解析 | 45 | 真实 Ctrl+C 文本 fixture；「普通复制不许带 T 级」「认不出必须显式失败」 |
| 词缀 T 级匹配 | 66 | 内存桩库，数值照抄真实库；多行匹配、药剂专页、中英文固有隐式区间与歧义拒答 |
| 装备分析报告 | 33 | 「不给稀有装备估值」「容量输入不完整不报余量」「跨页档位一致性与物等门槛」 |
| 实时经济价格、成本与双边价差 | 52 | 精确名称、批量清单加总、计价单位、联盟、TTL、缓存优先降级、exchange 与唯一装备 search/fetch、多币种换算、双边方向、整单深度、挂单时效、显式费用、跨联盟/无效/离群过滤、最低样本门槛与 unknown 降级 |
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
economy/                poe.watch / GGG Trade API 适配、领域模型、SQLite 快照/历史趋势与降级策略
orchestrator/           主循环、出口绑定闸门、引用装配
                          loop.py / binding.py / citation.py
agents/                 领域 Agent：提示词 + 允许工具集 + 输出契约
tools/                  工具注册与实现。顺序即推荐优先级：
                          economy_flip_analysis / economy_price_lookup / economy_price_trend
                          / economy_material_cost
                          / craft_attempt_plan / affix_pool_odds / check_mod_combination
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
                          / attempt_plan.py 独立选择几何分布、材料预算与价格快照一致性
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
                          / accuracy.py 命中率 / attack_speed.py 攻速
                          / dps.py 命中、穿刺、异常与技能 DoT / dps_analysis.py 比较与反解）
parsers/                输入解析：装备文本、词缀档位匹配、分析报告
                          item_text.py（纯文本，不连库）/ item_mods.py / mod_bridge.py / item_report.py
entities/               实体模型、分类体系、词缀反查
kb/                     采集管线与各来源抽取器（poedb / pob）
retrieval/              FTS5、别名、混合检索融合、分词、领域词表
static/                 单页前端：index.html / app.js / unique-history.js / style.css（无构建步骤）
tests/unit/             单元测试（unittest）
tests/golden/           黄金问答集、录制回放、人工审计结论、指标基线
scripts/                初始化、体检、验证、导入、评测
config.yaml             声明式行为：路由前缀、检索参数、来源优先级、领域词表
.env                    密钥（不入版本库）
data/                   SQLite 单文件 + HTTP 缓存（不入版本库）
logs/                   按日期命名的日志（不入版本库）
```

持久状态集中在 `data/poe.db`；运行中不要直接复制 WAL 主文件。使用 SQLite Backup API
生成一致性单文件快照，恢复前再校验清单和数据库完整性：

```bash
# macOS / Linux；Windows 把解释器换成 .venv\Scripts\python.exe
.venv/bin/python scripts/db_backup.py create
.venv/bin/python scripts/db_backup.py list
.venv/bin/python scripts/db_backup.py verify <list 返回的完整 .db 文件名>

# 默认只显示计划，不改文件
.venv/bin/python scripts/db_backup.py restore <备份名>
# 停止服务、确认计划无误后才显式执行；当前库会先自动备份
.venv/bin/python scripts/db_backup.py restore <备份名> --apply
```

快照与 `.manifest.json` 清单保存在 `paths.backups`（默认 `data/backups/`）。清单记录数据库
SHA-256、大小、schema SHA-256、SQLite/app 版本、页信息和关键表行数；`verify` 还会实时运行
`PRAGMA quick_check`。恢复拒绝路径逃逸、符号链接、哈希/schema/行数不符的快照，也拒绝目标旁
存在 `-wal`、`-shm` 或 `-journal`——这通常说明应用未完成优雅关停。恢复用目标目录临时文件
和 `os.replace` 原子替换；若当前库存在，替换前必先创建 `pre-restore` 安全快照。

带 `ADMIN_TOKEN` 的管理端提供 `GET /admin/backups`、`POST /admin/backups` 和
`POST /admin/backups/{backup_name}/verify`。管理 API **不提供 restore**：远程请求不能安全证明
当前进程已经停止。定时备份可调用创建端点或离线 CLI，但应同时把 `data/backups/` 复制到
另一块磁盘/对象存储；本机快照不能替代异地备份。

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
- **隐式词缀仍拿不到 T 级。** 底材库已入库（18 类 1022 条），其中 PoB 有英文固有隐式 536 条；
  缓存的 POEDB 中文类别页有 519 条能与同底材的数值序列核对。已有库离线回填会跳过
  1 条译名漂移，当前可用 518 条；刷新整张底材表后可得到 519 条。
  中英文都只在整条原文与可变数值区间吻合时给出范围和 roll；腐化/影响隐式不能借
  固有隐式推断。类别页没有隐式 T 级，所有隐式 T 级继续标「未匹配」。
- **具体底材页只在证据闭合时使用。** 本地 1022 条底材中，458 条护甲类有可唯一
  推导的属性标签且 458/458 个目标词缀页存在；13 条 Ward/特殊护甲类没有对应
  属性页，会拒绝定档。四个中文底材名重名：同页可定页面，跨页不选一件。
  药剂 48 件中 42 件可定页，6 件 Hybrid 缺词缀页保持未知；已识别底材也不会用相似页面代替。
- **PoB Build 体检只判不重算。** 分享码与白名单托管链接都能解析；核心 16 项、
  防御补充和单体 / 清图 / Boss 输出分析都只消费 Build 自带的 `PlayerStat` 与配置。
  旧版分享码没导出的字段会标「未知」；Boss 敌人档位未显式保存时也标「未知」；
  清图只列覆盖指标，不给强弱评级。报告里的 DPS 仍是复述 PoB 的结果；命中 DPS
  分析套件只处理调用方显式给出的单技能参数，不会从 Build 自动汇总修正或切换敌人配置。
  装备侧防御来源只列原文，不求和；
  双档建议只对有明确数值与上限的短板触发，成本是改造规模而非实时通货价。成长规划
  只比较用户显式数值目标与同一份 PlayerStat，并按用户优先级排序；不推断目标、具体改装或可达性。
- **Build 版本核对使用当前激活天赋方案。** 多套天赋树时按 `<Tree activeSpec>` 选择，
  不再固定读取第一套。若 Build 没有 `treeVersion` 或本地库没有版本标记，会明确显示
  “无法判断”，不会默认当成同版。
- **角色 API 需要用户自己的 GGG OAuth token。** 当前只支持 PoE1 PC realm，且不会
  自动刷新 token；`POE_OAUTH_USER_AGENT` 必须符合 GGG 的
  `OAuth clientId/version (contact: contact)` 格式。
- **截图识别共用 DeepSeek 模型。** 可用 `llm.vision_enabled` 关闭；模型只转录可见内容，
  用户确认后才进入确定性分析。图片不落盘，单张上限 5 MB。
- **升级前的旧会话无法自动确定所有者。** 数据库迁移会保留它们但不放进任何调用方的
  服务端列表；持有原随机会话 ID 的浏览器可首次认领。没有该 ID 时不会猜测归属或跨用户
  展示。新会话从创建起按 API Key 摘要隔离；未配置 Key 的本机模式统一属于 `anonymous`。
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
- **向量与远程重排通道关闭。** 单模型模式不调用独立的 Embedding / ReRank 模型；描述性查询仍依赖 `search_keywords` 的覆盖质量。
- **普通复制的混合词缀分组仍有边界。** 中文相邻两至三行可在 POEDB 整条词缀
  唯一匹配时合并；四行以上、英文普通复制、来源或底材档位/方向有歧义、多个候选分组
  共用一行时都不会自动合并。普通复制本身不标记词缀边界，即使完整组合与库吻合，
  仍可能是碰巧相邻的独立词缀；合并告警会提示用 Ctrl+Alt+C 进阶复制核对。
  进阶复制多行整条查不到时不会用首行的 T 级代替整条。
- **技能石 / 万神殿 / Boss 都是部分覆盖，不是未入库。** 技能石与万神殿已经有中文
  数据和结构化出口；Boss 当前只有 PoB 的伤害建模与倍率，没有机制说明和应对打法。
- **装备报告的剩余名额要求输入完整。** `/item` 与 `check_mod_combination` 已共用
  `craft_affix_capacity`；普通装备、药剂、珠宝、Graft 与已腐化深渊珠宝按对应规则
  判断。但存在未识别显式词缀、隐匿占位、未鉴定状态或未归类行时只报告已识别数量，
  不声称还剩几个位置。
- **PoB 历史变体只支持明确的命名变体对比。** 重新运行新版导入器后，旧版词缀会进入
  隔离表；不会从 `Pre X` 标签推断某个具体补丁的精确数值。未能从上游唯一确定的历史
  底材返回未知；多个当前选择型变体也不会自动折成单一“现版”。
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
