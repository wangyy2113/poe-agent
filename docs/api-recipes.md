# Web 操作与 API 示例

[← 项目首页](../README.md) · [文档导航](README.md)

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
