# POE1-Agent 需求文档（Requirements）

## 简介

POE1-Agent 是一套面向《流放之路 1（Path of Exile 1，POE1）》的领域 AI Agent 系统。它不是通用聊天机器人，而是以 **知识检索（Retrieval）+ 工具调用（Tool Calling）+ 推理（Reasoning）** 为核心的专家系统，为玩家提供 **可信、可追溯、可执行** 的专业建议。

### 核心约束：无据不答（No Evidence, No Answer）

系统的第一原则是：**没有依据就不回答，而不是给一个可能错的答案。**

POE1 的机制高度耦合——一条词缀的适用条件、一个技能标签的有无、一次取整方式的差异，都可能让整条结论反向。因此本系统**禁止**基于 LLM 参数记忆的经验推断（Inference）。宁可明确回答「查不到」，也不输出未经证实的内容。

这条原则按内容类型分级执行：

| 内容类型 | 定义 | 规则 |
| --- | --- | --- |
| **事实断言**（Factual Claim） | 游戏机制、词缀、数值、标签、版本改动 | **必须**逐条绑定引用或工具输出。无依据即不输出该断言，改为缺据声明（Evidence Gap Notice）。**禁止**推断、估算、类推。 |
| **计算派生**（Derivation） | 由已引用事实 + 计算器工具得出的结论 | 允许，但**必须**展示完整推导链，且链上每个输入都可溯源。这是可复核的演算，不是推断。 |
| **建议**（Recommendation） | 优化方向、制作路线、投资与规划建议 | 允许，但**必须**建立在有据事实之上，显式标注为建议并写出假设。**禁止**在建议中夹带无依据的事实断言。 |

「不回答」的粒度是**单条断言**，不是整轮回答：能答的部分照答并附引用，答不了的部分逐条列出缺什么、可去哪里查证。这样既不输出错误内容，也不因局部缺口废掉整个回答。

### 术语表（Glossary）

| 术语 | 含义 |
| --- | --- |
| KB | Knowledge Base，知识库 |
| RAG | Retrieval-Augmented Generation，检索增强生成 |
| BD / Build | 角色构筑方案 |
| PoB | Path of Building，离线构筑规划器 |
| POEDB | 流亡编年史 <https://poedb.tw/cn/>，一级数据源 |
| eHP | Effective HP，有效生命值 |
| Max Hit | 可承受的单次最大伤害 |
| Tier / T 级 | 词缀等级 |
| Chunk | 知识文档切片，检索的最小单元 |
| Citation | 引用，指向具体知识来源的可点击溯源信息 |
| RRF | Reciprocal Rank Fusion，倒数排名融合 |
| 事实断言 | Factual Claim，关于游戏机制/数值/词缀/版本的陈述，必须有据 |
| 断言绑定 | Claim-Citation Binding，每条事实断言与其引用的强制对应关系 |
| 缺据声明 | Evidence Gap Notice，知识库无覆盖时输出的明确「查不到」说明 |
| 缺口台账 | Knowledge Gap Ledger，记录所有缺据声明以驱动知识库补全 |
| 漏出 | Leakage，未绑定引用的事实断言进入最终答案，属严重缺陷 |

### 基线事实（Baseline Facts，2026-08-04 实测）

以下事实已通过实际请求验证，作为需求与设计的前提：

| 数据源 | 状态 | 结论 |
| --- | --- | --- |
| `poedb.tw/cn/` | 200 可用 | 一级知识源 |
| `pathofexile.com/api/leagues` | 200 可用 | 当前赛季 = **Allflame**（2026-07-24 开始） |
| `pathofexile.com/api/trade/data/{stats,items,static}` | 200 免鉴权 | 词缀/底材字典源 |
| `api.poe.watch/leagues` | 200 可用 | **经济数据主源** |
| poe.ninja 旧 `/api/data/*` | **全部 404（已下线）** | 不可作为经济数据接口依赖 |
| PathOfBuilding 仓库 | 活跃（2026-08-03） | 结构化数据 + 计算参考实现 |
| RePoE | **停更（2024-12）** | 不可作为新版本数据源 |
| `poe-tool-dev/dat-schema` | 活跃（2026-08-02） | `.dat` 提取路径可用 |

---

## 需求列表

### 需求 1：基于检索的游戏知识问答

**用户故事：** 作为 POE1 玩家，我希望就任意游戏机制、装备、技能、赛季内容提问并得到准确答案，以便我不必在多个网站间反复查证。

#### 验收标准

1. WHEN 用户提交游戏知识类问题 THEN 系统 SHALL 先执行知识检索，再基于检索结果生成答案。
2. WHEN 系统输出任何事实断言 THEN 该断言 SHALL 绑定至少一条引用（Citation，含来源站点、条目标题、可访问 URL）或一条工具输出记录。
3. IF 某个待回答的事实点在知识库中检索不到足够依据 THEN 系统 SHALL 输出缺据声明，SHALL NOT 输出该事实点的任何推断性内容，且 SHALL NOT 伪造引用。
4. 缺据声明 SHALL 包含：明确的「知识库中查不到」表述、缺失的具体信息点、建议的查证途径（如对应 POEDB/Wiki 页面链接或搜索关键词）。
5. WHEN 用户问题包含多个事实点且仅部分有依据 THEN 系统 SHALL 回答有依据的部分并附引用，同时逐条列出无依据的部分，SHALL NOT 因局部缺口拒绝整轮回答，也 SHALL NOT 用推断填补缺口。
6. WHEN 用户问题涉及具体数值（如词缀数值范围、技能系数、取整方式） THEN 系统 SHALL 引用知识库中的原始数值，SHALL NOT 自行估算、取近似值或按同类物品类推。
7. WHEN 系统输出计算派生结论 THEN 系统 SHALL 展示完整推导链，且链上每个输入值 SHALL 标注其引用来源或产出该值的工具。
8. WHEN 系统输出建议类内容 THEN 该内容 SHALL 被显式标注为建议，其依赖的事实前提 SHALL 各自带引用，所作假设 SHALL 被写出。
9. WHEN 检索结果中 POEDB 与其他来源存在事实冲突 THEN 系统 SHALL 并列展示双方描述，并说明「当前更推荐采用 POEDB」，SHALL NOT 自行判定谁对而隐藏另一方。
10. IF 用户明确要求系统给出无依据的猜测 THEN 系统 SHALL 拒绝，并说明本系统不提供未经证实的游戏机制结论。
11. WHEN 用户以中文提问 THEN 系统 SHALL 以中文回答，并保留关键术语的英文原文以便玩家对照社区资料。
12. WHEN 用户提出与 POE1 无关的问题 THEN 系统 SHALL 说明超出服务范围，SHALL NOT 进入通用闲聊模式。

---

### 需求 2：知识库构建与数据源治理

**用户故事：** 作为系统维护者，我希望知识库能够从权威来源自动构建并保持可更新，以便回答始终基于最新版本数据。

#### 验收标准

1. 知识库 SHALL 按以下优先级组织数据源：P1 POEDB、P2 Path of Building 社区版数据（`src/Data/`，由 GGG 客户端 `.dat` 导出）、P3 PoE Wiki、P4 Craft of Exile、P5 经济数据源、P6 GGG 官方 Patch Notes、P7 Reddit 等社区讨论。
   - PoB 数据置于 P2 而非 P1：它在**数值与结构**上更接近一手（自 `.dat` 机器导出），但不含中文名与页面说明，且社区分支存在人工修补。因此 POEDB 仍为最高优先级（需求 15.5），冲突时推荐 POEDB。
   - 引用 PoB 时 URL SHALL 指向**钉住 commit 的** GitHub blob 地址，使引用可点击且可复现；SHALL 同时记录 PoB 的 `manifest` 版本与天赋树版本。
2. P1–P6 来源 SHALL 可用于支撑事实断言。P7 社区来源 SHALL NOT 用于支撑任何事实断言，仅可在明确标注为「社区观点」时用于建议类内容；WHEN 引用 P7 内容 THEN 系统 SHALL 说明该内容未经权威来源证实。
3. WHEN 系统在同一问题上检索到不同优先级来源 THEN 排序 SHALL 优先采信高优先级来源。
4. 每一份知识文档 SHALL 携带完整 Metadata：`title`、`category`、`source`、`url`、`league`、`version`、`language`、`tags`。
5. IF 某文档缺少 `url` 或 `source` THEN 该文档 SHALL NOT 进入可引用索引。
6. 知识库 SHALL 至少覆盖以下一级分类：游戏基础、装备、制作系统、地图系统、联盟机制、经济系统。
7. WHEN 抽取器解析页面时关键字段缺失或结构不符预期 THEN 该条目 SHALL 标记为抽取失败并被排除在索引之外，SHALL NOT 以部分字段或占位值写入知识库。
8. WHEN 采集器抓取外部站点 THEN 系统 SHALL 遵守目标站点 `robots.txt`、限制并发与请求速率、携带可识别 User-Agent，并对响应做本地缓存以避免重复请求。
9. WHEN 同一 URL 的内容与上次抓取一致 THEN 系统 SHALL 跳过重新切片与重新嵌入（增量更新）。
10. WHEN 游戏发布新版本或新赛季 THEN 系统 SHALL 支持在不重建全库的前提下增量更新受影响分类，并更新文档的 `version` 与 `league` 字段。
11. 系统 SHALL 记录每次采集任务的来源、条目数、成功/失败数与耗时，供管理端查询。
12. WHEN 系统输出缺据声明 THEN 系统 SHALL 将该缺口写入缺口台账（问题、缺失信息点、推测所属分类、时间），供管理端按频次排序，用于指导后续采集优先级。

---

### 需求 3：混合检索与重排

**用户故事：** 作为玩家，我希望无论用专有名词（如「猎首」）还是自然语言描述（如「怪物死亡后偷取词缀的头盔」）提问，都能检索到正确条目。

#### 验收标准

1. 系统 SHALL 同时提供关键词检索（BM25）与语义向量检索（Vector Search），两者 SHALL 均在应用进程内完成，SHALL NOT 依赖独立部署的检索服务。
2. WHEN 执行检索 THEN 系统 SHALL 融合 BM25 与向量检索结果（RRF 或加权融合），可选经 ReRank 重排后返回。
3. 向量检索 SHALL 使用支持中英文的多语言嵌入模型（如 `bge-m3`），该模型 SHALL 通过 OpenAI 兼容接口远程调用，SHALL NOT 要求本地常驻推理服务。
4. WHEN 对中文文本建立 BM25 索引 THEN 系统 SHALL 使用中文分词，SHALL NOT 按字符或空格切分。
5. WHEN 用户查询包含精确实体名（装备名、技能名、Boss 名） THEN 系统 SHALL 保证该实体的规范条目出现在候选集中（实体名精确匹配通道）。
6. 检索结果 SHALL 支持按 `category`、`league`、`version` 过滤。
7. 系统 SHALL 提供独立的检索接口，使检索质量可脱离 LLM 单独评测。
8. WHEN 检索耗时超过配置上限 THEN 系统 SHALL 返回已获得的部分结果而非整体失败。
9. 检索结果 SHALL 携带每条候选的相关度分数与来源优先级，供上层判定其是否构成「充分依据」。
10. WHEN 检索因超时而降级为部分结果 THEN 该轮判定 SHALL 采用更保守策略：宁可输出缺据声明，也不基于不完整候选集作答。
11. 向量检索与 ReRank SHALL 可通过配置独立关闭；WHEN 两者关闭或其远程接口不可用 THEN 系统 SHALL 退化为「BM25 + 实体精确匹配」并在检索元数据中标注当前生效的检索通道。
12. WHEN 向量检索被关闭 THEN 系统 SHALL 通过实体的描述性关键词字段补偿语义检索能力，使自然语言描述类查询仍可经 BM25 命中。

---

### 需求 4：Agent 编排与工具调用

**用户故事：** 作为玩家，我希望提出复合问题（例如「我这套 BD 打 Uber Sirus 缺什么，预算 50 div」）时，系统能自动拆解任务、调用多个能力并给出整合结论。

#### 验收标准

1. WHEN 收到用户请求 THEN 编排器（Orchestrator）SHALL 依次完成：意图理解 → 计划制定 → 工具选择 → 知识检索 → 推理 → 答案组织。
2. 系统 SHALL 提供可注册的工具集，至少包含：知识检索、装备解析、PoB 解析、计算器、经济查询。
3. WHEN 单个问题需要多种能力 THEN 编排器 SHALL 能在一次会话轮次内调用多个工具并合并结果。
4. IF 某个工具调用失败或超时 THEN 系统 SHALL 降级为可用能力继续作答，并在回答中标注哪部分能力不可用。
5. 系统 SHALL 记录每轮的完整执行轨迹（意图、计划、工具调用入参与出参、检索命中、耗时），并可按会话 ID 追溯。
6. WHEN 工具返回结构化数值 THEN 最终答案中的对应数值 SHALL 与工具输出一致，SHALL NOT 被 LLM 改写。
7. 系统 SHALL 对单轮的工具调用次数与总 Token 消耗设上限，防止失控循环。

---

### 需求 5：OpenAI 兼容 API 与后端服务

**用户故事：** 作为开发者，我希望通过标准接口接入 POE1-Agent，以便复用现有客户端生态。

#### 验收标准

1. 系统 SHALL 暴露 OpenAI 兼容的 `/v1/chat/completions` 接口，支持 `stream=true` 的流式响应。
2. 系统 SHALL 提供业务接口：`/chat`、`/search`、`/build`、`/item`、`/craft`、`/calculator`、`/index`、`/admin`。
3. WHEN 请求 `/index` 或 `/admin` 下的任何端点 THEN 系统 SHALL 校验管理凭证，未通过校验 SHALL 返回 401/403。（这两组接口可触发采集与索引重建，必须鉴权，不得公开暴露。）
4. WHEN 上游 LLM 服务报错 THEN 系统 SHALL 返回结构化错误（含错误码与可读原因），SHALL NOT 泄露密钥或内部堆栈。
5. LLM 供应商 SHALL 通过配置切换（Base URL + API Key + 模型名），SHALL NOT 硬编码在代码中。LLM 与 Embedding/ReRank SHALL 为各自独立的配置项，允许指向同一网关，但 SHALL NOT 在代码中假定两者必然同源。
6. LLM 客户端 SHALL 支持多 API Key 按优先级故障转移：WHEN 某 Key 因额度耗尽或鉴权/模型权限失败 THEN 系统 SHALL 将其暂时移出轮转并切换到下一个可用 Key；IF 失败为 5xx / 429 等瞬时错误 THEN 系统 SHALL 仅重试，SHALL NOT 降级该 Key。
7. WHEN 某 Key 绑定了特定模型 THEN 切换到该 Key 时 SHALL 同时切换为其绑定模型。
8. 日志与错误响应中的 API Key SHALL 脱敏，完整 Key SHALL NOT 出现在任何输出中。
9. LLM 调用 SHALL 支持流式输出与工具调用（Tool Calling），SHALL NOT 采用无法获取增量输出的接入方式——因为断言绑定校验依赖按句处理生成结果。
10. 系统 SHALL 提供 `/healthz` 健康检查，反映数据库、向量通道、LLM 通道与 Embedding 通道的可用状态。
11. 所有密钥 SHALL 仅从环境变量或密钥文件读取，SHALL NOT 提交进版本库。

---

### 需求 6：Chat Web 前端

**用户故事：** 作为玩家，我希望有一个类似 ChatGPT 的网页界面，能贴装备、贴 PoB 码、看到带引用的流式回答。

#### 验收标准

1. 前端 SHALL 支持 Markdown 渲染与代码高亮。
2. 前端 SHALL 以流式方式逐步渲染回答。
3. WHEN 用户粘贴（Ctrl+V）游戏内 Ctrl+C 复制的装备文本 THEN 前端 SHALL 自动识别为装备并触发装备分析流程。
4. 前端 SHALL 支持粘贴或输入 PoB 分享码/链接以触发 Build 分析。
5. 前端 SHALL 支持图片上传，用于装备截图与 BD 截图。
6. 前端 SHALL 展示回答中的引用，引用 SHALL 可点击跳转至来源 URL。
7. 前端 SHALL 持久化对话历史，支持会话切换与重命名。
8. WHEN 后端返回缺据声明 THEN 前端 SHALL 以视觉上明确可辨的样式呈现「知识库查不到」及其建议查证途径，SHALL NOT 将其混排为普通答案文本。
9. WHEN 答案含建议类内容 THEN 前端 SHALL 将其与事实性内容在视觉上区分开。
10. 前端 SHALL 在移动端与桌面端均可正常使用。

---

### 需求 7：装备文本解析与分析

**用户故事：** 作为玩家，我希望把游戏里 Ctrl+C 的装备文本贴进来，立刻知道它值不值钱、值不值得继续做。

#### 验收标准

1. WHEN 输入为游戏装备文本 THEN 系统 SHALL 解析出：物品类别、稀有度、底材（Base Item）、物品等级、品质、插槽与连接、隐式词缀、显式词缀、腐化状态、影响词（Influence）、裂隙/合成标记。
2. 系统 SHALL 识别特殊词缀类型：隐式（Implicit）、显式（Explicit）、腐化隐式（Corrupted Implicit）、裂隙（Fractured）、合成隐式（Synthesised）、附魔（Enchant）、工艺台词缀（Crafted）。
3. WHEN 解析出显式词缀 THEN 系统 SHALL 标注每条词缀的 T 等级（Tier）与该词缀在当前底材上的数值范围。
4. 系统 SHALL 输出装备的基础分析、词缀分析、市场价值参考、可制作方向。
5. 系统 SHALL 给出以下判断及其理由：是否值得裂界（Fracture）、是否值得镜像湖（Reflecting Mist）、是否达到镜子级别（Mirror Worthy）。这些判断属建议类内容，其依据的事实前提 SHALL 各自带引用。
6. IF 装备文本格式无法识别 THEN 系统 SHALL 返回明确的解析失败原因与期望格式示例，SHALL NOT 猜测内容继续分析。
7. WHEN 市场价值来自外部经济数据 THEN 系统 SHALL 标注数据来源、时间戳与所属赛季。
8. IF 经济数据源中查不到该物品或该配置的报价 THEN 系统 SHALL 声明无价格数据，SHALL NOT 依据同类物品类推或凭经验给出价格区间。
9. IF 某条词缀无法在知识库中匹配到对应词缀实体 THEN 系统 SHALL 标注该词缀为「未匹配」并说明无法给出 T 级与数值范围，SHALL NOT 猜测其 T 级。

---

### 需求 8：PoB 导入与 Build 分析

**用户故事：** 作为玩家，我希望导入我的 PoB 后得到一份体检报告，明确告诉我短板在哪、怎么改、要花多少。

#### 验收标准

1. 系统 SHALL 支持以下 Build 输入方式：PoB 分享码、PoB 托管链接、天赋树（Passive Tree）链接、角色 API 导入、截图。
2. WHEN 解析 PoB THEN 系统 SHALL 提取：职业、升华（Ascendancy）、等级、主技能、技能组配置、装备、珠宝、天赋树、药剂、万神殿（Pantheon）、光环与保留（Reservation）、诅咒（Curse）。
3. WHEN 输出 Build 总览 THEN 系统 SHALL 包含：主技能、职业、升华、等级、DPS、Effective HP、Max Hit。
4. WHEN 执行 Build 分析 THEN 系统 SHALL 检查并报告下列全部项目，不得遗漏：DPS、Effective HP、Max Hit、抗性、混沌抗性、法术压制（Spell Suppression）、护甲（Armour）、闪避（Evasion）、格挡（Block）、药剂（Flask）、保留（Reservation）、诅咒（Curse）、万神殿（Pantheon）、珠宝（Jewels）、天赋树（Passive Tree）、升华（Ascendancy）。
5. 防御分析 SHALL 额外覆盖：最大抗性、吸纳（Recoup）、偷取（Leech）、回复（Recovery）。
6. 输出分析 SHALL 区分：单体 DPS、清图能力（Clear Speed）、Boss 输出（Boss Damage）。
7. WHEN 发现短板 THEN 系统 SHALL 给出优化建议，并至少分为「低预算方案」与「高预算方案」两档，每条建议 SHALL 说明预期收益与大致成本。
8. IF PoB 数据中某项指标缺失或不可解析 THEN 系统 SHALL 在报告中标注该项为「未知」，SHALL NOT 用估计值冒充实测值。
9. WHEN 用户提供的 PoB 版本与系统数据版本不一致 THEN 系统 SHALL 提示可能存在的偏差。

---

### 需求 9：制作规划（Craft Agent）

**用户故事：** 作为玩家，我希望告诉系统目标装备和预算，它给我一条可执行、有成功率和成本的制作路线。

#### 验收标准

1. 系统 SHALL 接受输入：目标装备（含目标词缀）、预算、已有词缀/已有底材。
2. WHEN 生成制作方案 THEN 系统 SHALL 输出：制作路线（Craft Path）、逐步步骤（Step）、每步成功率（Probability）、平均成本（Average Cost）、替代方案（Alternative Strategy）。
3. 系统 SHALL 覆盖主流制作体系：精华（Essence）、化石（Fossil）、收获（Harvest）、辛迪加（Betrayal）、野兽（Bestiary）、神庙（Incursion）、工艺台（Crafting Bench）。
4. WHEN 计算成本 THEN 系统 SHALL 使用当前赛季的实时通货价格，并标注价格时间戳。
5. IF 目标词缀组合在当前版本机制下不可能同时存在（如同组互斥词缀） THEN 系统 SHALL 明确指出不可行及其原因。
6. WHEN 输出成功率 THEN 该概率 SHALL 来自可查证的词缀权重数据或制作模拟工具，并 SHALL 标注计算依据与数据来源。
7. IF 缺少该制作路径所需的词缀权重或概率数据 THEN 系统 SHALL 明确说明无法给出成功率，SHALL NOT 凭经验给出概率数字或「大概」「通常」类模糊量化。
8. WHEN 输出平均成本 THEN 其所依赖的通货单价 SHALL 来自经济数据源并标注时间戳；IF 单价缺失 THEN SHALL 说明成本不可计算。
9. IF 预算不足以完成目标 THEN 系统 SHALL 给出预算内的降级目标方案。

---

### 需求 10：经济分析（Economy Agent）

**用户故事：** 作为玩家，我希望知道当前赛季什么东西在涨、什么能赚钱。

#### 验收标准

1. 系统 SHALL 支持查询以下品类价格：唯一装备（Unique）、通货（Currency）、圣甲虫（Scarab）、命运卡（Divination Card）、碎片（Fragment）。
2. WHEN 展示任何价格 THEN 系统 SHALL 标注所属赛季、数据来源与数据时间戳。
3. 系统 SHALL 提供价格趋势（Trend）、利润分析（Profit）与 Flip 建议。
4. 经济数据 SHALL 通过可替换的数据源适配层接入；WHEN 主数据源不可用 THEN 系统 SHALL 自动降级到备用源，并在回答中说明当前实际使用的来源。
   > 依据基线事实：poe.ninja 旧公开 API 已下线，主源采用 `api.poe.watch`，poe.ninja 仅作为人工参考链接。
5. 系统 SHALL 缓存经济数据并遵守上游速率限制，缓存有效期可配置。
6. WHEN 数据陈旧超过配置阈值 THEN 系统 SHALL 在回答中给出陈旧警告。
7. WHEN 给出 Flip 或投资建议 THEN 系统 SHALL 同时说明风险与假设前提，SHALL NOT 表述为保证收益。

---

### 需求 11：Boss 攻略（Boss Agent）

**用户故事：** 作为玩家，我希望在打某个 Boss 前知道它会怎么杀我，以及我该准备什么。

#### 验收标准

1. 系统 SHALL 覆盖主要终局 Boss：Maven、Sirus、Shaper、Elder、Uber Elder、Cortex、Searing Exarch、Eater of Worlds 及其 Uber 版本。
2. WHEN 用户询问某 Boss THEN 系统 SHALL 输出：危险机制（Dangerous Mechanics）、推荐防御（Recommended Defense）、推荐 DPS 门槛、药剂配置（Flask）、打法流程（Strategy）。
3. WHEN 用户已导入 Build THEN 系统 SHALL 结合该 Build 的实际数值给出针对性提示（例如指出其 Max Hit 不足以承受特定技能）。
4. WHEN 提到具体伤害数值或机制细节 THEN 系统 SHALL 附引用。
5. IF 某 Boss 机制在近期版本有改动 THEN 系统 SHALL 说明改动内容与生效版本。

---

### 需求 12：游戏计算器（Calculator）

**用户故事：** 作为玩家，我希望能直接算出抗性、暴击、保留、Max Hit 等数值，而不用手动套公式。

#### 验收标准

1. 系统 SHALL 提供以下计算器：抗性（Resistance）、吸纳（Recoup）、偷取（Leech）、暴击（Critical Strike）、保留（Reservation）、Max Hit、持续伤害（DOT）、感电（Shock）。
2. 每个计算器 SHALL 实现为无副作用的纯函数，并 SHALL 有单元测试覆盖边界条件。
3. WHEN 返回计算结果 THEN 系统 SHALL 同时返回计算过程与所用公式，使结果可复核。
4. IF 输入参数超出游戏合法范围（如抗性上限、保留超过 100%） THEN 系统 SHALL 返回校验错误并说明合法范围。
5. WHEN 计算依赖游戏常量（如基础暴击倍率、抗性上限） THEN 该常量 SHALL 集中定义、标注来源与适用版本。
6. WHEN LLM 需要给出数值结论 THEN 其 SHALL 调用计算器工具，SHALL NOT 自行心算。

---

### 需求 13：规划能力（Planner Agent）

**用户故事：** 作为玩家，我希望在赛季开始时得到一条从开荒到终局的清晰路线。

#### 验收标准

1. 系统 SHALL 支持开荒路线规划（League Starter Planning），输出分阶段目标（升级、装备、技能、关键节点）。
2. 系统 SHALL 支持 Atlas 天赋规划（Atlas Planning），并根据用户目标（刷图速度、通货收益、特定材料）给出不同配点方向。
3. WHEN 用户说明自身目标（开荒 / 冲榜 / 刷图 / 打 Boss / 赚钱） THEN 系统 SHALL 输出与该目标匹配的个性化路线，而非通用模板。
4. WHEN 输出路线 THEN 每个阶段 SHALL 包含可验证的完成标志（如「点数达到 X」「抗性已满」）。
5. WHEN 规划涉及具体机制或收益数据 THEN 系统 SHALL 附引用。

---

### 需求 14：版本与改动分析（Patch Analysis）

**用户故事：** 作为玩家，我希望知道新版本改了什么、对我的 BD 有什么影响。

#### 验收标准

1. 系统 SHALL 索引 GGG 官方 Patch Notes，并保留版本号与发布日期。
2. WHEN 用户询问某机制的历史变化 THEN 系统 SHALL 按版本时间线给出变更点及来源。
3. WHEN 用户已导入 Build 且询问版本影响 THEN 系统 SHALL 指出该 Build 中受改动影响的具体部分。
4. WHEN 知识库中同一实体存在多个版本的描述 THEN 系统 SHALL 默认采用当前版本，并在必要时说明历史差异。

---

### 需求 15：证据约束（Agent 行为强约束）

**用户故事：** 作为使用者，我需要能完全信任这个系统输出的每一个事实。POE1 机制耦合极深，一个错误的小前提会让整条结论反向，所以我宁愿它告诉我「查不到」，也不要一个听起来合理但可能错的答案。

#### 验收标准

1. 系统 SHALL 优先查询知识库，SHALL NOT 依赖 LLM 参数记忆回答任何游戏机制问题。
2. **系统 SHALL NOT 输出经验推断内容。** 具体禁止：无引用支撑的机制描述、凭记忆给出的数值、按同类物品类推的属性、未经证实的版本改动、无数据支撑的概率与价格。
3. IF 缺少依据 THEN 系统 SHALL 输出缺据声明，SHALL NOT 以「据我所知」「一般来说」「通常」「大概」等模糊限定语输出未经证实的内容。
4. 每条事实断言 SHALL 与其引用形成可校验的绑定关系；未绑定引用的事实断言 SHALL 被系统在输出前拦截，此拦截 SHALL 由代码执行而非依赖模型自觉。
5. POEDB SHALL 具有最高来源优先级；WHEN Wiki 与 POEDB 冲突 THEN 系统 SHALL 分别陈述两者描述并推荐采用 POEDB。
6. WHEN 陈述任何游戏机制 THEN 系统 SHALL 提供出处，格式形如「来源：POEDB → 猎首（Headhunter）」并附 URL。
7. WHEN 执行 Build 分析 THEN 系统 SHALL 完整覆盖需求 8.4 列出的检查项，SHALL NOT 遗漏；IF 某项数据缺失 THEN SHALL 标注为「未知」而非填入估计值。
8. WHEN 系统给出的结论基于假设（如假定满抗、假定某药剂生效） THEN 该假设 SHALL 被显式写出。
9. 系统 SHALL NOT 将 P7 社区来源作为事实依据；WHEN 引用社区观点 THEN SHALL 标注为未经证实的社区讨论且仅用于建议类内容。
10. 计算派生结论 SHALL 与推断严格区分：派生 SHALL 展示完整推导链且每个输入可溯源，SHALL NOT 在链中引入无据前提。
11. WHEN 知识库中存在同一实体的多版本描述且无法确定适用于当前版本 THEN 系统 SHALL 说明该不确定性并列出各版本描述，SHALL NOT 自行选定一个版本作为事实陈述。
12. 系统 SHALL NOT 因为「拒答显得无用」而放宽以上任何一条；输出错误内容的代价高于输出「查不到」。

---

### 需求 16：非功能需求

#### 验收标准

1. **轻量部署**：系统 SHALL 以单进程方式运行，SHALL NOT 依赖任何需独立启动的外部服务（数据库服务、向量库服务、本地推理服务、前端 Node 进程）。部署 SHALL 仅需：建虚拟环境 → 装依赖 → 启动单个 Python 进程。
2. **可配置性**：模型、嵌入模型、检索参数、来源优先级、缓存时长 SHALL 通过配置文件或环境变量调整，无需改代码。
3. **响应性能**：纯知识问答的**正文**首字节响应时间 SHALL 在正常网络下不超过 5 秒；检索阶段耗时 SHALL 可被单独观测。WHEN 使用推理模型且推理阶段尚未结束 THEN 前端 SHALL 显示「思考中」状态，SHALL NOT 以空白等待。
4. **成本可控**：系统 SHALL 记录每轮 Token 消耗（**含推理 token**），并支持配置单轮上限。
   > 实测 GLM 5.2 在极简任务上推理 token 占输出总量 98%，只统计正文会严重低估成本。
5. **推理内容隔离**：WHEN 模型返回推理内容（如 `reasoning_content`） THEN 系统 SHALL NOT 将其作为答案内容输出给用户，SHALL NOT 使其参与断言绑定校验。推理过程属模型内部猜测，透出即绕过证据约束。
6. **输出预算充足**：`max_tokens` SHALL 留足推理预算；IF 正文为空而推理 token 大于零 THEN 系统 SHALL 记录明确告警而非静默返回空答案。
7. **可评测性**：系统 SHALL 提供黄金问答集（Golden Set）与自动化评测脚本，SHALL 同时度量以下四项指标：
   - 检索命中率（Recall@k）
   - 引用正确率（引用是否真实支撑该断言）
   - **无据断言漏出率**（未绑定引用的事实断言进入答案的比例，目标值 0）
   - **错误拒答率**（知识库实际有据却输出缺据声明的比例）

   > 后两项必须成对监控。只压漏出率会让系统退化为「什么都不答」，只压拒答率会让推断内容重新漏出。
8. **数据合规**：采集外部站点 SHALL 遵守 robots.txt 与站点条款，限速并缓存；采集到的原始数据 SHALL NOT 提交进版本库。
9. **安全**：`/admin` 与 `/index` SHALL 鉴权；密钥 SHALL 仅来自环境变量；错误响应 SHALL NOT 泄露内部实现细节。
10. **本地化**：默认输出语言为简体中文，关键术语保留英文原文。
11. **可观测性**：系统 SHALL 输出结构化日志，包含会话 ID、工具调用、检索命中与耗时，便于排障与质量分析。
12. **跨平台可移植**：系统 SHALL 能在 Windows、macOS、Linux 上以相同方式运行，迁移 SHALL 不需要修改应用代码。具体要求：
    - 应用代码 SHALL 保持平台中立；所有平台差异 SHALL 收敛到启动与进程托管层
    - SHALL NOT 硬编码解释器路径或使用 `/` 拼接路径；路径操作 SHALL 使用 `pathlib`
    - SHALL NOT 在应用代码中调用平台专有命令（`ps`、`grep`、`awk`、`kill`、`nohup` 等）
    - SHALL NOT 使用仅 Unix 可用的模块（`fcntl`、`os.fork`、`resource`、`pwd`、`grp`、`termios`）
    - 信号处理 SHALL 对平台不存在的信号（如 `SIGHUP`）做存在性判断
    - 所有文本文件读写与日志输出 SHALL 显式指定 UTF-8 编码，SHALL NOT 依赖平台默认编码
    - 环境初始化脚本 SHALL 以 Python 实现为唯一真源，各平台的 `.sh` / `.bat` 仅作薄封装
    - 全部第三方依赖 SHALL 在目标平台有可安装分发（wheel 或纯 Python 源码包）
13. **依赖可降级**：IF 某项可选能力在当前环境不可用（如 SQLite 扩展加载被禁用、扩展无对应架构的 wheel、网关未注册 embedding 模型） THEN 系统 SHALL 在启动时探测并自动禁用该能力，记录原因，SHALL NOT 启动失败。

---

## 范围划分（Scope by Version）

| 版本 | 范围 | 对应需求 |
| --- | --- | --- |
| **V1** | POEDB 知识库、中文问答、Chat Web、混合检索、引用溯源、装备识别 | 1、2、3、5、6、7、15、16 |
| **V2** | PoB 分析、Build Advisor、Craft Advisor、Boss Advisor、计算器、角色 API 导入 | 4、8、9、11、12、13 |
| **V3** | 多 Agent 协同、MCP 工具调用、知识库自动同步、实时经济分析、版本自动对比、长期记忆、Build 成长规划 | 10、14 及全部需求的增强 |

## 明确的非目标（Non-Goals）

1. 不做游戏客户端插件、内存读取或任何可能违反 GGG 使用条款的自动化操作。
2. 不做自动交易、自动挂机或代打。
3. 不重新实现 PoB 的完整伤害计算引擎；复杂数值以解析 PoB 输出为准，自建计算器只覆盖需求 12 列出的独立公式。
4. 不承诺经济预测的准确性，价格相关输出均为参考。
5. **不提供猜测。** 本系统不做「虽然查不到但我猜是这样」的服务。用户若需要未经证实的推测，应转向社区讨论渠道；本系统只回答查得到的内容。
6. 不追求「什么问题都能答」的覆盖率指标。覆盖不足的正确应对是补知识库，不是放宽证据要求。
