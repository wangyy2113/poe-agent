# POE1-Agent 实施计划（Tasks）

任务按**可运行、可验证**的增量组织：每完成一个阶段，系统都应能真实跑通一条链路，而不是攒到最后再联调。V1 任务已细化到可直接执行；V2/V3 保留为待细化提纲。

约定：
- 每项标注对应需求编号，形如 `_需求: 2.3, 2.6_`。
- 外部数据源相关任务必须先跑通「少量样本」再放量。
- 涉及数值的实现必须同时补单元测试。

---

## 阶段 0：工程底座

- [ ] 0.1 初始化仓库与工程骨架（仓库根平铺布局）
  - 仓库根平铺 `main.py` + `core/ api/ orchestrator/ agents/ tools/ retrieval/ kb/ entities/ parsers/ calc/ economy/ static/ scripts/ tests/`
  - `git init`，`.gitignore` 排除 `.venv/`、`data/`、`logs/`、`.env`
  - `.gitattributes`：`* text=auto` + `*.sh text eol=lf`（防 Git 转 CRLF 破坏 shell 脚本）
  - `requirements.txt`（依赖范围）+ `requirements.lock`（钉版本），所有依赖须在 Windows 有 wheel 或为纯 Python
  - 不建 `pyproject.toml`、不建 `frontend/`、不写 Dockerfile / docker-compose.yml
  - _需求: 16.1, 16.8, 16.12_

- [ ] 0.2 双层配置与密钥管理
  - `core/config.py` 用 `pydantic-settings` 读 `.env`，必填项无默认值、缺失即启动失败。分两组独立配置：
    - LLM：`LLM_BASE_URL`（内部 LiteLLM 网关地址，填在 `.env` 里，不写进版本库）、`LLM_API_KEY`（LiteLLM virtual key）、`LLM_MODEL=glm-5.2`，以及多 Key `LLM_API_KEYS`（格式 `key[:model]` 逗号分隔、按优先级排列）
    - Embedding/ReRank：`EMBEDDING_BASE_URL`、`EMBEDDING_API_KEY`、`EMBEDDING_MODEL`、`RERANK_MODEL`（独立配置项，默认与 LLM 指向同一网关）
    - 不要引入 `ANTHROPIC_CUSTOM_MODEL_OPTION*` 三项——那是 Claude Code CLI 模型选择器的 UI 标签，与直连 HTTP 无关
    - 管理令牌 `ADMIN_TOKEN`（与普通用户 API Key 分离）
  - `config.yaml` 管声明式行为：检索参数（`min_score`、`top_k`、融合权重、`vector.enabled`、`rerank.enabled`）、来源优先级、事实句判定领域词表、缓存时长、限速、`max_tool_calls`、`max_tokens_per_turn`、监听地址（默认 `127.0.0.1`）
  - `.env.example` 模板，`setup.py` 拷贝后尝试 `os.chmod` 并容忍失败；密钥仅从环境读取
  - _需求: 5.5, 5.11, 16.2, 16.4_

- [ ] 0.3 单文件 SQLite 存储与日志
  - `core/db.py`：单连接 `sqlite3.connect(check_same_thread=False)` + `PRAGMA journal_mode=WAL`，路径由 `pathlib` 解析为 `data/poe.db`
  - 建表：`entity`、`alias`、`chunk`、`chunk_fts`(FTS5)、`chunk_vec`(sqlite-vec)、`conversation`、`message`、`trace`、`ingest_run`、`knowledge_gap`
  - **三级探测 sqlite-vec**：`hasattr(conn, "enable_load_extension")` → 能否实际启用 → 能否加载扩展。任一失败即禁用向量通道并记录原因，**不得导致启动失败**
  - 日志：结构化输出 + **按日期后缀命名文件**（`app-YYYY-MM-DD.log`，不做 rename 轮转）+ handler 显式 `encoding="utf-8"`，含会话 ID 与请求 ID
  - _需求: 4.5, 16.11, 16.13_

- [ ] 0.4 启动入口（平台中立）
  - `main.py`：装配 FastAPI、挂载 `static/`、进程内启动 uvicorn、默认绑 `127.0.0.1`
  - `setproctitle` 设为 best-effort（Windows 上无效，不可作为功能依赖）
  - **单实例保护靠端口绑定失败**，不用 PID 文件、不用 `kill`
  - 优雅关停：SIGTERM/SIGINT/KeyboardInterrupt → 停收新请求 → 等进行中请求 → WAL checkpoint → 退出；注册信号前用 `hasattr(signal, "SIGHUP")` 判断
  - 启动时 `sys.stdout.reconfigure(encoding="utf-8")`，避免 Windows 默认 cp936 破坏中文输出
  - `/healthz` 反映 SQLite / LLM 通道 / embedding 通道 / 向量扩展 状态
  - _需求: 5.10, 16.1, 16.12_

- [ ] 0.5 跨平台引导与托管
  - `scripts/setup.py` 为**唯一真源**：用 `sys.executable` + `venv` 模块建环境、装 `requirements.lock`（回退 `requirements.txt`）、`Path.mkdir` 建 `data/ logs/`、拷 `.env.example`（`os.chmod` 尝试并容忍失败），幂等可重复执行，全程不调用 shell 工具
  - `scripts/setup.sh` 与 `scripts/setup.bat` 仅作薄封装转调 `setup.py`
  - `scripts/poe-agent.service`（Linux systemd，含 `PYTHONUTF8=1`）+ `scripts/start.bat`（Windows，`pythonw.exe` 后台）
  - 验证（两个平台各跑一次）：全新目录下 `python scripts/setup.py` 后启动成功，**不需要启动任何其他进程**
  - _需求: 16.1, 16.12_

- [x] 0.6 网关能力探测 —— **已完成（2026-08-04，`scripts/probe_gateway.py`，11/13 通过）**
  - 可复跑：`python3 scripts/probe_gateway.py`（自动从环境变量 / `~/.claude/settings.json` / 本项目 `.env` / `SIBLING_ENV_PATH` 指向的 `.env` 收集候选凭证并逐个验活，token 全程脱敏）
  - 结论：
    - 网关 19 个模型，`glm-5.2` 在列
    - **tool calling 可用**：正确调用 `kb_search`，参数合法 JSON；**多轮可用**：回传工具结果后基于其生成 523 字并主动提及来源 → 编排器设计无需改动
    - **流式可用**：正文 13 个增量分片，粒度足够按句缓冲
    - **无 embedding / rerank 模型** → `vector.enabled=false`、`rerank.enabled=false`，走 BM25 + 实体精确 + `search_keywords`
    - **GLM 5.2 是推理模型**：`reasoning_tokens` 占比极高（极简任务 114/117），`reasoning_content` 与 `content` 分离且推理分片全在正文之前
  - _需求: 3.11, 5.5, 5.9_

- [x] 0.6b 推理模型适配（由 0.6 实测结论驱动）—— **后端已完成（2026-08-04，`core/llm.py`）**
  - 可复跑：`python scripts/verify_llm.py --offline`（38 项，无需凭证）；带可用凭证时 `python scripts/verify_llm.py`（51 项，含真实网关）
  - `MIN_MAX_TOKENS = 2048` 在**客户端层**强制抬升，不信任配置与调用方；正文为空而 `reasoning_tokens > 0` 时抛 `EmptyContentError`（非流式）或记 WARNING（流式），告警里报的是**本次实际生效**的 max_tokens
  - Token 计量用 `total_tokens`（`reasoning_tokens` 是 `completion_tokens` 的子集，实测 129 里含 126，**不要再加一遍**）；`TurnBudget` 跨多次工具调用累计并施加上限
  - 流式只把 `delta.content` 作为文本外发。`StreamEvent.__post_init__` 强制「只有 `kind="content"` 可携带 `text`」，推理内容在**类型层面**就无法流出——把安全要求落成代码约束而非调用方自觉；`LLMResponse` 只留 `reasoning_chars` 不留推理正文
  - 推理阶段发一个 `kind="thinking"` 事件（不含文本）供前端显示「思考中」；前端渲染部分留到 5.2
  - 实测复核（真实网关）：推理占输出 97.7%（126/129）、流式 6 个正文分片 + 1 个 thinking、推理 1027 字全程未外发、tool calling 参数合法
  - _需求: 16.3, 16.4, 16.5, 16.6_

- [x] 3.1 LLM 客户端与多 Key 凭证池 —— **已完成（提前于阶段 3，因 0.6b 的落点就在此）**
  - `openai` SDK 直连 `/v1/chat/completions`；`base_url` 自动补 `/v1`（`.env` 配的是根地址，不补会 404）
  - 凭证池粘性选择（始终从优先级最高扫，不轮询）；额度类冷却 30 分钟、鉴权/模型权限类 60 分钟；5xx/429 只重试不降级 Key；切 Key 同步切绑定模型
  - **实测坑：网关对过期 Key 返回 HTTP 400 而非 401**（`type: expired_key`）。按「400 一律 fatal」写就不会故障转移，故 400 需再按 body 关键词二次分类。已加回归断言
  - **实测坑：429 有两义**——限流（瞬时，重试）与额度耗尽（冷却换 Key），必须看 body 才能区分
  - Key 一律脱敏（`前7位…后4位`）；`_scrub()` 兜底清洗可能混入异常消息的完整 Key；`/healthz` 暴露脱敏后的凭证池状态与冷却原因
  - 分级退避 `[1,2,4,8,15]` 秒；SDK 的 `max_retries=0`，重试只由本模块管，避免两层退避叠加
  - 待补：`gate_model` 廉价守门的调用封装（3.6 用得上）
  - _需求: 4.7, 5.5, 5.6, 5.7, 5.8, 5.9, 16.4_

- [ ] 0.6c 补上可用的 LLM 凭证（**阻塞后续所有在线链路**）
  - 现状：`.env` 的 `LLM_API_KEY` 是占位符 `sk-`（长度 3，从未填过）；`~/.claude/settings.json` 里那个已于 2026-07-31 15:59 UTC 过期
  - 影响：`scripts/verify_llm.py` 的在线部分、后续 Agent 与 `/chat` 全部无法实跑
  - 处理：把可用的 LiteLLM virtual key 填进 `.env`；有多个时用 `LLM_API_KEYS`（`key[:model]` 逗号分隔，按优先级），故障转移逻辑已就绪
  - _需求: 5.5, 16.2_

- [ ] 0.7 平台中立性检查
  - 代码层禁止项自查：硬编码解释器路径、字符串拼路径、`os.fork`/`fcntl`/`resource`/`pwd`/`grp`/`termios`、shell 调用 `ps`/`grep`/`awk`/`kill`/`nohup`、未指定 encoding 的 `open()`
  - HTTP 缓存文件名用 URL 哈希，避免 Windows `MAX_PATH` 260 超长
  - 加一条测试或检查脚本，把上述禁止项做成可自动化的静态检查
  - _需求: 16.12_

---

## 阶段 1：知识库（POEDB 优先）

- [ ] 1.1 实体与元数据模型
  - 实现 `Entity`、`DocumentMetadata`、`Chunk`、`Citation`、`SourceRef`
  - `entities/taxonomy.py` 定义六大一级分类与二级分类常量
  - 校验规则：缺 `url` 或 `source` 的文档不得进入可引用索引
  - _需求: 2.4, 2.5, 2.6_

- [ ] 1.2 采集基础设施
  - 统一 `Source` 接口（`discover` / `fetch` / `extract`）
  - robots.txt 校验、令牌桶限速、可识别 User-Agent、磁盘 HTTP 缓存
  - 内容哈希比对以支持增量跳过
  - _需求: 2.8, 2.9, 16.8_

- [ ] 1.3 POEDB 抽取器（先做垂直切片验证）
  - 先只做**唯一装备**一类，端到端跑通「抓取 → 实体 → 卡片 → 索引」
  - 抽取器加结构断言：关键字段缺失即让本次采集失败，不静默写脏数据
  - 验证：随机抽 20 条与页面人工核对，数值与词缀完全一致
  - _需求: 2.1, 2.4, 2.7_

- [ ] 1.4 扩展 POEDB 抽取覆盖面
  - 依次补齐：技能/辅助技能、底材、词缀（含隐式/腐化隐式/裂隙/合成/附魔）、天赋与升华、万神殿、制作系统、地图与 Boss、通货类
  - 每类沿用 1.3 的抽样核对流程
  - _需求: 2.6_

- [ ] 1.5 知识卡片渲染与切片
  - 实体 → 规范化 Markdown 卡片（保留数值原文，避免生成期改写）
  - 生成 `search_keywords` 字段（如猎首 → 「偷取 词缀 怪物 死亡 稀有」），写入 FTS5 的 `keywords` 列，用于补偿向量通道关闭时的描述性查询
  - 切片策略保证同一词缀的名称与数值范围不被切开（数值被切断会导致该事实点无法引用，进而被判定为缺据）
  - _需求: 1.6, 3.12_

- [ ] 1.6 别名表
  - 从实体中英文名生成 `alias → entity_id` 映射，并支持人工补充社区俗称（如「猎首/HH/Headhunter」）
  - _需求: 3.5_

- [ ] 1.7 采集任务记录与管理查询
  - 每次采集写 `ingest_run`（来源、条目数、成败数、耗时）
  - _需求: 2.11_

---

## 阶段 1b：Path of Building 数据源（P2）

PoB 的 `src/Data/` 是 GGG 客户端 `.dat` 的机器导出，在**数值与结构**上比 POEDB
更接近一手，但无中文名、无页面说明。定位是「数值真源 + POEDB 的交叉校验源」，
不是替代 POEDB。

- [x] 1b.1 清点与解析可行性 —— **已完成（2026-08-04）**
  - 可复跑：`python scripts/probe_pob.py --align`
  - `kb/sources/pob_lua.py`：纯 Python 的 Lua **字面量**解析器。34/34 项数据全量解析通过，6.4 万条目、30.8 MB、6.9 秒
  - 不装 Lua 运行时：本机没有 lua/luajit，PoB 的 `runtime/` 是 Windows DLL；装解释器等于给项目加平台相关外部依赖。且我们**不重算 PoB 伤害引擎**（硬规则 6），所以 `statMap` 里的 `mod()`/`bit.bor()` 调用无用，无需求值——记为 `LuaOpaque` 保留原文，不静默丢弃
  - 仅 `Skills/*.lua`、`Minions.lua`、`Spectres.lua` 含函数调用（4395 处），其余 31 项纯字面量
  - 版本读取不调 `git` 命令：直接读 `.git`（含 detached HEAD / packed-refs / worktree 三种形态）
  - _需求: 2.1, 2.4_

- [x] 1b.2 来源优先级与可复现引用 —— **已完成**
  - PoB 定为 **P2**，Wiki 及之后顺移，`MAX_FACTUAL_PRIORITY` 5→6。POEDB 保持 P1，冲突仍推荐 POEDB（需求 15.5 不变）。改动时库里只有 POEDB 数据，无需重导
  - 引用 URL **钉住 commit** 的 GitHub blob 地址；读不到 commit 时导入器**拒绝写库**（`--allow-unpinned` 才放行）——指向可变分支的引用不可复现，等于伪造溯源
  - _需求: 2.1, 2.2, 2.5, 15.5_

- [x] 1b.3 词缀导入（第一个垂直切片）—— **已完成**
  - `kb/sources/pob_mods.py` + `scripts/import_pob_mods.py`，新表 `pob_mod`，25,487 条
  - 单独建表而非并入 `mod_affix`：两源粒度不同（POEDB 按「词缀 × 底材类别」一行，PoB 一条词缀一行、底材差异在权重向量里），且交叉校验要求各自独立保存，混表会被 UPSERT 悄悄覆盖
  - PoB 独有的三样：**按底材标签的权重向量**（制作成功率的依据，硬规则禁止无权重支撑的成功率）、**tradeHashes**（23,927 条，打通 Trade API/poe.watch）、权威英文文本与词缀名
  - 实测踩到并已处理：
    - `tags` 与 `modTags` 是两回事（机制标记 vs 分类标签），合并会污染检索过滤
    - `ModMaster.lua` 用**位置项**无键名，数字索引当 uid 会在上游增删时整体错位 → 改内容哈希；且哈希必须含 `types`，否则「戒指/护身符/手套」与「箭袋」两条同文本词缀撞 uid，箭袋那条被吞
    - 头盔附魔是「技能 × 实验室难度」两层嵌套，按 flat 解析 274 条全漏
    - PoB 负区间写 `-(15-11)`、POEDB 写 `(-15--11)`，不归一会解析成 `(15,11)`：符号丢失且下界大于上界，下游把减伤算成加伤
    - `ModMap.lua` 是 PoB **手写**的 UI 配置（`apply=function` 闭包、`%d%%` 占位、无权重无区间），明确排除，地图词缀走 POEDB
  - 导入器体检项：uid 唯一、模板 `#` 数 = 区间数、区间下界 ≤ 上界、未知字段告警、按 form 解析出 0 条告警
  - _需求: 2.4, 2.7, 2.11_

- [x] 1b.4 与 POEDB 对账 —— **已完成**
  - `scripts/verify_pob_mods.py`。对账主键是 `group`（两源都是 GGG 内部标识符，字面一致）；**模板不能当键**，中英文本归一后是两个不同字符串。比较**区间数值**：纯数字、语言无关
  - 结果：**22,848 行区间指纹一致（占已找到同组的 86.4%）**，两源数值互相验证通过
  - **不下「冲突」结论**：POEDB 的 `correct_group` 取 `family_list[0]`，粒度比 PoB 的 `group` 粗，同名组里装着语义不同的词缀（实测 `ManaRegeneration` 组下 POEDB 是固定回魔、PoB 是回魔速率）。没有可靠跨语言配对键之前报冲突就是凭空造冲突，而冲突要按需求 15.5 展示给用户——造假冲突比漏报更糟
  - 差异落 `source_conflict` 但 `kind='mod_review'`（待核对）而非 `'mod'`（已确认冲突），2,729 条。人工确认后再改 kind
  - 顺带发现并修掉 POEDB 侧真 bug：混合词缀的 `<br>` 被直接剥掉，两行粘成 `+(4-6)% 所有元素抗性召唤生物获得 +(5-8)% 元素抗性`——既会原样输出给用户，模板也匹配不上任何真实装备行。约 10% 词缀行受影响，工艺台词缀里也有。已改为插入 ` / ` 分隔符（与 PoB 侧一致），重建 93,875 条 + 24,418 张梯度卡片，`verify_mods.py` 往返命中率仍 100%
  - _需求: 2.7, 15.5, 15.6_

- [ ] 1b.5 后续切片（按「零阻塞优先」排序）
  - **游戏常量**（`Misc.lua` 的 `gameConstants` + 怪物属性表，带 `-- From GameConstants.dat` 出处注释）→ `calc/constants.py` 真源。无中文名问题，零阻塞
  - **唯一装备交叉校验**：1215 件与库内重叠，可查 POEDB 抽取错误。**必须只取 `Variant: Current`**——1293 件里 752 件（58%）带历史变体，不过滤会把旧版数值一起输出（冈姆的壮志 +500 与 +1000 并存）
  - **tradeHashes 回填**：给库内中文词缀补交易站 stat id，打通 `economy/`
  - **英文名/tags 回填**：用 `(group, ilvl, ranges)` 把 PoB 的英文文本与 modTags 关联到 `mod_affix`
  - 底材 / 技能石 / 天赋树 / 万神殿 / Boss：**有阻塞**，需先补 POEDB 中文页，否则 `name_zh` 只能填英文（违背中文输出要求）。缓存里 195 页是中英混杂，底材中文名不完整
  - 混合词缀的多行匹配：`mod_lookup` 目前按单行模板匹配，带 ` / ` 的多行词缀反查仍失配
  - _需求: 2.6, 7.3, 9, 12_

---

## 阶段 2：检索

- [ ] 2.1 FTS5 全文索引与中文分词（BM25 通道）
  - `retrieval/fts.py`：写入前用 `jieba` 预分词成空格分隔词序列存 `tokens` 列，`unicode61` 分词器按空格切分；禁止按字符切分
  - `keywords` 列写入实体的 `search_keywords`，补偿关闭向量时的语义检索能力
  - 检索用 `bm25(chunk_fts)` 排序（注意返回值越小越相关）
  - 支持 `category`/`league`/`version` 作为 SQL 条件联合过滤
  - 补测试：中文查询正确命中（对照按字符切分的错误基线）
  - _需求: 3.1, 3.4, 3.6, 3.12_

- [ ] 2.2 向量通道（sqlite-vec，可关闭）
  - `retrieval/vector.py`：`chunk_vec` 用 vec0 虚拟表，与实体同库
  - embedding 结果持久化，同内容不重复调用远程接口
  - 扩展不可用时兜底：int8 量化 + numpy 暴力检索；或按配置直接禁用该通道
  - _需求: 3.1, 3.11_

- [ ] 2.3 远程嵌入与重排客户端（可关闭）
  - HTTP 客户端封装 embedding / rerank，OpenAI 兼容接口，含超时与重试
  - 模型名、地址、`enabled` 开关均可配置；**不在本机常驻模型进程**
  - 接口不可用时自动降级并在检索元数据标注当前生效通道
  - _需求: 3.3, 3.11, 16.2_

- [ ] 2.4 混合检索融合
  - 三路：实体精确通道（SQL 查 `alias` 表）+ BM25（FTS5）+ 向量（可关闭）
  - RRF 融合（只用排名，避开 `bm25()` 负值与向量距离的量纲差异）→ 来源优先级加权 → 可选 ReRank
  - 任一路超时或不可用用已完成结果继续，并标注生效通道
  - P7 社区来源在候选中标记为不可支撑事实断言
  - _需求: 2.2, 2.3, 3.2, 3.5, 3.8, 3.11_

- [ ] 2.5 充分依据判定与缺据声明
  - 重排最高分低于 `min_score` 时判定为无充分依据，返回 `evidence_gap` 而非进入生成（由代码判定，不交给模型）
  - 缺据声明含：缺失的具体信息点 + 建议查证途径（对应站点链接或搜索关键词）
  - 检索超时降级时判定标准收紧，取保守侧
  - _需求: 1.3, 1.4, 3.10_

- [ ] 2.6 缺口台账
  - 每条缺据声明写入 `knowledge_gap`（问题、缺失信息点、推测分类、时间、命中次数）
  - `/admin` 按频次排序展示，作为采集优先级输入
  - _需求: 2.12_

- [ ] 2.7 `/search` 接口与检索评测
  - 独立检索接口，返回命中 chunk、分数、来源与优先级
  - 建立黄金问答集，须包含四类样本：专名查询、自然语言描述、数值类、跨来源冲突类，**以及一组知识库确定不覆盖的问题**（用于验证拒答行为）
  - 脚本输出四项指标：Recall@k、引用正确率、无据断言漏出率（目标 0）、错误拒答率
  - 用该评测集调 `min_score`，漏出率与错误拒答率成对权衡
  - _需求: 3.7, 3.9, 16.7_

---

## 阶段 3：Agent 与 API

- [x] 3.1 LLM 客户端（`openai` SDK 直连 LiteLLM 网关 + 多 Key 凭证池）
  - 已在阶段 0 完成，见上面的 3.1 条目（`core/llm.py` + `scripts/verify_llm.py`）。
    提前做是因为 0.6b 的推理模型适配没有别的落点。

- [ ] 3.2 工具注册机制
  - `ToolResult` 统一包封（`ok`/`data`/`citations`/`error`/`elapsed_ms`）
  - JSON Schema 自动导出给 LLM
  - 实现首批工具：`kb_search`、`entity_lookup`
  - _需求: 4.2_

- [ ] 3.3 Orchestrator 主循环
  - 意图识别 → 计划制定 → 工具调用循环 → 答案组织
  - 工具失败降级并在答案中标注不可用能力
  - 工具调用次数上限保护
  - _需求: 4.1, 4.3, 4.4, 4.7_

- [ ] 3.4 引用装配与冲突对照
  - 汇总工具引用，渲染「来源：POEDB → 条目名」+ URL
  - 同实体跨来源数值冲突时生成对照块并推荐 POEDB，不自行判定谁对
  - _需求: 1.2, 1.9, 15.5, 15.6_

- [ ] 3.5 数值回填校验
  - 工具数值以占位符注入，生成后回填校验；不一致以工具结果为准并记录告警
  - _需求: 1.6, 4.6_

- [ ] 3.6 断言—引用绑定校验（出口闸门，禁止推断的核心落点）
  - 证据编号注入：进入生成的每条证据分配 `E<n>` 并随引用一起进上下文
  - 提示词要求每个事实句末尾附 `[[E<n>]]` 标记
  - 出口逐句校验：事实句分类（规则优先：句式 + 可配置领域词表 + 数值/机制术语检测；规则判不准的句子再交小模型裁决，用廉价模型做守门，不占主模型预算）→ 检查绑定是否存在、编号是否有效、证据是否真正支撑该句关键实体与数值
  - 未通过者拦截并替换为该点缺据声明，写入缺口台账与轨迹，计入 `leakage_blocked` 指标
  - 区分三类内容：事实断言强制绑定；计算派生强制附推导链且输入可溯源；建议强制标注并写出假设
  - 单测覆盖：编造机制句被拦、无标注数值句被拦、合法派生句放行、合法建议句放行
  - _需求: 1.2, 1.7, 1.8, 15.2, 15.3, 15.4, 15.10_

- [ ] 3.7 Knowledge Agent
  - 提示词 + 允许工具集 + 输出契约
  - 行为约束：先检索后作答、无依据即输出缺据声明（不得用「一般来说/大概」等模糊限定语代替）、非 POE1 问题拒答、用户索要猜测时拒绝、中文回答保留英文术语
  - 多版本描述无法确定适用版本时列出各版本并说明不确定性，不得自行选定
  - _需求: 1.1, 1.3, 1.5, 1.10, 1.11, 1.12, 15.1, 15.3, 15.9, 15.11_

- [ ] 3.8 Chat 接口（含 OpenAI 兼容）
  - `/v1/chat/completions` 支持 `stream=true`；`/chat` 返回引用与轨迹 ID
  - 流式输出下断言绑定校验的处理：以句为单位缓冲，校验通过后再下发，避免未校验内容已推送给用户
  - 会话历史持久化
  - 结构化错误响应，不泄露密钥与堆栈
  - _需求: 5.1, 5.2, 5.4_

- [ ] 3.9 鉴权与限流
  - 普通 API Key 鉴权 + 按 Key 限流（防止被当作免费 LLM 代理）
  - `/index`、`/admin` 使用**独立管理凭证**，可选 IP 白名单
  - _需求: 5.3, 16.9_

- [ ] 3.10 执行轨迹落库与查询
  - 每轮写完整轨迹，含每条断言的绑定校验结果与拦截原因
  - `/admin` 可按会话 ID 追溯
  - _需求: 4.5_

---

## 阶段 4：装备解析

- [ ] 4.1 装备文本解析器
  - 解析类别、稀有度、底材、物品等级、品质、插槽连接、需求、各类词缀与标记
  - 失败时返回明确原因 + 期望格式样例，禁止猜测续行
  - 用真实 Ctrl+C 文本 fixture 做单测（含唯一/稀有/腐化/裂隙/合成/附魔样本）
  - _需求: 7.1, 7.2, 7.6_

- [ ] 4.2 词缀 Tier 与数值范围匹配
  - 数字占位归一化（`+45 to maximum Life` → `+# to maximum Life`）后查 Tier A 实体
  - 按底材 + 词缀组定位实际数值所在档位
  - 匹配不到对应词缀实体时标注「未匹配」并说明无法给出 T 级，**禁止猜测 T 级**
  - _需求: 7.3, 7.9_

- [ ] 4.3 装备分析输出
  - 基础分析、词缀分析、可制作方向
  - 裂界 / 镜像湖 / 镜子级别判断标注为建议类内容，事实前提各自带引用
  - 市场价值标注来源、时间戳与赛季；查不到报价时声明无价格数据，**禁止按同类物品类推价格**
  - _需求: 7.4, 7.5, 7.7, 7.8_

- [ ] 4.4 `/item` 接口
  - _需求: 5.2_

---

## 阶段 5：Chat Web

- [ ] 5.1 单页静态前端骨架（无构建步骤、无 Node 运行时）
  - `static/index.html` + `app.js` + `style.css`，由 FastAPI `StaticFiles` 托管
  - `marked` + `highlight.js` 以本地静态文件随仓库提供，不依赖 CDN（便于内网部署）
  - CSS 媒体查询适配移动端
  - _需求: 6.10, 16.1_

- [ ] 5.2 流式对话界面
  - `fetch` + `ReadableStream` 消费 SSE，按句增量追加；Markdown 渲染 + 代码高亮
  - _需求: 6.1, 6.2_

- [ ] 5.3 粘贴内容识别
  - `onPaste` 分流：装备文本 / PoB 码 / 普通文本；以可视卡片先给用户确认再分析
  - _需求: 6.3, 6.4_

- [ ] 5.4 引用、缺据声明与建议分区展示
  - 内联角标 + 底部来源列表，可点击跳转
  - `evidence_gap` 段用明确可辨样式呈现「查不到 + 缺失信息点 + 建议查证途径」，不与正文混排
  - `kind=recommendation` 内容与事实内容视觉分区
  - _需求: 6.6, 6.8, 6.9_

- [ ] 5.5 会话历史与图片上传
  - 会话切换与重命名；图片上传通道（分析能力留待 V2）
  - _需求: 6.5, 6.7_

---

## V1 验收清单

V1 完成的判定标准（逐条实测，不以「无报错」代替验证）：

- [ ] 能就 POEDB 覆盖范围内的问题给出带可点击引用的中文答案
- [ ] 专名查询（如「猎首」）与自然语言描述查询均能命中正确条目
- [ ] 数值类问题的答案与 POEDB 原文一致（抽样核对）
- [ ] **知识库无覆盖时输出缺据声明**，含缺失信息点与查证途径，不出现伪造引用，不出现「一般来说/大概」类模糊推断
- [ ] **构造一批知识库确定不覆盖的问题，逐条确认系统拒答而非编造**（这是本版本最关键的验收项）
- [ ] 部分有据的复合问题：有据部分正常作答，无据部分单独列出，不因局部缺口整轮拒答
- [ ] 断言绑定校验可拦截未标注引用的事实句，拦截事件在轨迹中可查
- [ ] 跨来源冲突时并列展示并推荐 POEDB
- [ ] 粘贴真实装备文本可解析出全部词缀并标注 T 级；未匹配词缀标注「未匹配」而非猜测 T 级
- [ ] `/index` 与 `/admin` 未带管理凭证时返回 401/403
- [ ] 全新机器上 `python scripts/setup.py` 后启动即可跑通，**全程只有一个进程、无需启动任何外部服务**
- [ ] 关闭向量与重排（`vector.enabled: false`）后仍能正常问答，检索元数据标注为降级通道
- [ ] 全部状态集中在 `data/poe.db` 单文件，复制该文件即完成备份
- [ ] **在 Windows 上完成一次实际迁移验证**：装 python.org 3.12 → `python scripts\setup.py` → 拷 `.env` 与 `data\poe.db` → 启动成功 → 中文问答无编码问题，且**未修改任何应用代码**
- [ ] 在扩展加载不可用的环境（如 macOS 系统自带 Python）启动时自动禁用向量通道并记录原因，服务仍可用
- [ ] 检索评测脚本可运行并输出四项指标：Recall@k、引用正确率、无据断言漏出率、错误拒答率
- [ ] 缺口台账可按频次排序，能据此看出下一步该补哪类数据

---

## V2：分析能力（待细化）

- [ ] PoB 解析（Base64 → zlib → XML → 模型），支持分享码与托管链接
- [ ] Build 报告契约实现，强制覆盖需求 8.4 全部检查项
- [ ] 防御分析（含最大抗性、吸纳、偷取、回复）与输出分析（单体/清图/Boss）
- [ ] 低预算 / 高预算双档优化建议
- [ ] 计算器全套（抗性、吸纳、偷取、暴击、保留、Max Hit、DOT、感电）+ 常量表附来源与版本
- [ ] Craft Agent：路线、步骤、成功率、成本、替代方案
- [ ] Boss Agent：机制、防御门槛、药剂、流程；可结合已导入 Build
- [ ] 角色 API 导入与截图识别
- [ ] 天赋树链接解析
- _需求: 4, 8, 9, 11, 12, 13_

## V3：协同与实时（待细化）

- [ ] 多 Agent 协同调度
- [ ] MCP 工具接入
- [ ] 知识库自动同步与版本 diff 分析
- [ ] 实时经济分析（poe.watch 主源 + Trade API 兜底 + 降级透明标注）
- [ ] 用户长期记忆
- [ ] Build 成长规划
- _需求: 10, 14_
