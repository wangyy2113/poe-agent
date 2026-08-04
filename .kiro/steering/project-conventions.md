# POE1-Agent 项目约定

本项目是面向《流放之路 1》的领域 AI Agent 系统。完整规格见 `.kiro/specs/poe1-agent/`（requirements / design / tasks）。

## 技术栈（不要随意替换）

部署形态：**单进程、单 SQLite 文件、零外部服务，不用容器。**

- 运行时：Python 3.12（最低 3.10），Windows / macOS / Linux 通用。用 python.org 官方版或 Homebrew Python，**不要用系统自带 Python**
- 依赖：`pip` + `venv`，`requirements.txt` + `requirements.lock`。**不用 uv / poetry / conda**，不建 `pyproject.toml`
- 后端：FastAPI + Pydantic v2 + uvicorn，在 `main.py` 进程内启动，默认只绑 `127.0.0.1`
- LLM：`openai` SDK 直连内部 LiteLLM 网关的 `/v1/chat/completions`，模型 **`glm-5.2`**，鉴权 `Authorization: Bearer <virtual key>`。网关地址由 `.env` 的 `LLM_BASE_URL` 提供，**不写进版本库**
  - **不引入 `anthropic` SDK**——该网关（LiteLLM 1.82.3）已同时提供 OpenAI 与 Anthropic 两套协议，我们用 OpenAI 那套即可
  - **不用 `claude` CLI 子进程模式**——拿不到增量流会破坏断言绑定校验，且会把 Node 依赖引回来。有些接法配 `ANTHROPIC_BASE_URL` 是因为 CLI 只会说 Anthropic 协议，那是 CLI 的约束不是网关的
  - **不要照搬 `ANTHROPIC_CUSTOM_MODEL_OPTION` / `_NAME` / `_DESCRIPTION`**——那是 Claude Code CLI 模型选择器的 UI 标签，直连 HTTP 时无意义。只需 `base_url` + `api_key` + `model` 三个值
- Embedding / ReRank：端点存在但**网关当前未注册任何 embedding / rerank 模型**（2026-08-04 实测），故 `vector.enabled=false`、`rerank.enabled=false`，检索走 BM25 + 实体精确 + `search_keywords`。将来若运维在 LiteLLM 上注册了 embedding 模型，只需改配置，代码无需改动
- 向量维度**不要写死**：建 `chunk_vec` 表前先探测 embedding 模型实际输出维度

### GLM 5.2 是推理模型——四条必须遵守的约定

实测（`scripts/probe_gateway.py`）：`reasoning_tokens` 占输出总量可达 98%，`reasoning_content` 与 `content` 字段分离，推理分片全部出现在正文之前。

1. **`max_tokens` 必须留足推理预算。** 实测「只回答两个字」消耗 117 输出 token，其中 114 是推理。设小了会**正文为空且不报错**——最隐蔽的失败模式。正文为空而推理 token 大于零时必须记告警。
2. **Token 计量计入 `reasoning_tokens`**，否则成本严重低估。
3. **流式只消费 `delta.content`**；`reasoning_content` 不参与断言绑定校验。
4. **绝不把推理内容展示给用户。** 这是安全要求：推理过程正是模型动用参数记忆猜测的地方，而那恰是「无据不答」要拦的东西。透出去等于绕过全部六道闸门。前端只显示「思考中」状态。
- 存储：**单文件 SQLite（WAL）** `data/poe.db`，承载实体库、FTS 索引、向量、会话、轨迹、缺口台账
- BM25：SQLite **FTS5 + 内置 `bm25()`**（已实测可用），写入前用 `jieba` 预分词成空格分隔词序列
- 向量：**`sqlite-vec`** 扩展，与实体同库；装不上则 int8 量化 + numpy 暴力检索兜底，或按配置关闭
- 嵌入 / 重排：远程 OpenAI 兼容接口调用，**可配置关闭**；不在本机常驻模型进程
- 前端：**单页静态**（`static/` 下 HTML + 原生 JS），FastAPI `StaticFiles` 托管。无 Node 运行时、无构建步骤、无框架
- 配置：`.env`（pydantic-settings 读密钥）+ `config.yaml`（声明式行为）
- 部署：`scripts/setup.sh` 初始化 + systemd unit（生产）+ `nohup`（开发兜底）

**不要引入**：Docker / docker-compose、Qdrant 或其他需独立启动的向量库、Postgres、Redis、本地推理服务、Node 前端工程。引入任何新依赖前先确认既有选型无法满足；新增依赖须钉确切版本、同步进 `requirements.lock`，并确认在 Windows 有 wheel 或为纯 Python。

## 跨平台硬规则（目标：迁到 Windows 不改一行应用代码）

应用代码必须平台中立，平台差异只允许出现在启动层与进程托管层（`scripts/`）。

**写代码时禁止**：

- 硬编码解释器路径（如 `/opt/homebrew/bin/python3.12`）→ 用 `sys.executable`
- 字符串拼接路径或硬编码 `/` → 一律 `pathlib.Path`
- `os.fork`、`fcntl`、`resource`、`pwd`、`grp`、`termios` 等 Unix 专有模块
- shell 调用 `ps` / `grep` / `awk` / `kill` / `nohup` 等平台工具
- 不带 `encoding` 的 `open()` → 一律显式 `encoding="utf-8"`（Windows 默认 cp936 会破坏中文）
- 依赖 rename 的日志轮转 → 按日期后缀直接命名（Windows 文件被占用时 rename 会 `WinError 32`）
- 无条件注册 `SIGHUP` → Windows 上不存在，须 `hasattr(signal, "SIGHUP")` 判断
- 用 PID 文件 + kill 做单实例保护 → 靠 HTTP **端口绑定失败**判断，跨平台且天然可靠
- 用 URL 原文做缓存文件名 → 用哈希，避开 Windows `MAX_PATH` 260 限制

**平台差异既成事实（实测，勿凭记忆改）**：

- FTS5 编译在 SQLite 内，所有平台可用 → BM25 是唯一确定可用的检索通道，必须作为主通道
- sqlite-vec 是可加载扩展，依赖 `Connection.enable_load_extension`，**并非所有 Python 发行版都有**：Homebrew Python 3.12 有；macOS 自带 Python 3.9 编译带 `OMIT_LOAD_EXTENSION`，没有该方法
- sqlite-vec **只有 win_amd64，没有 win_arm64** → Windows on ARM 上向量通道不可用
- 故：启动时三级探测（属性存在 → 能启用 → 能加载），失败则禁用向量通道并记录原因，**不得让启动失败**
- 要求使用 python.org 官方版或 Homebrew Python，不要用系统自带 Python

`scripts/setup.py` 是环境初始化的唯一真源，`.sh` / `.bat` 只是薄封装。新增初始化逻辑写进 `setup.py`，不要只改某一个平台的封装脚本。

## 沿用的工程实践（别重新发明）

- **多 Key 故障转移**：凭证按优先级粘性选择第一个健康的；额度耗尽类冷却 30 分钟，鉴权/模型权限类冷却 60 分钟；5xx / 429 只重试**不降级** Key；切 Key 时同步切换其绑定模型（受限 Key 访问别的模型只会 401）。配置格式 `key[:model]` 逗号分隔。
- **Key 脱敏**：日志只输出 `前7位…后4位`，完整 Key 绝不进日志或错误响应。
- **分级退避**：`[1, 2, 4, 8, 15]` 秒，429 单独识别。
- **模型别名表**：别名 → 完整模型 ID，配置和运行时切换都用别名。
- **廉价模型做守门**：需要二次判断的场景（如事实句分类）用小模型，不占主模型预算；能用规则判的优先用规则。

## 目录约定

仓库根平铺，不做 `src/` 包布局、不打包安装，以仓库根为工作目录运行，配置里的路径都相对工作目录：`main.py` + `core/ api/ orchestrator/ agents/ tools/ retrieval/ kb/ entities/ parsers/ calc/ economy/ static/ scripts/ tests/`。

路由层（`api/`）只做校验与编排调用，不写业务逻辑。抓取产物、SQLite 文件、日志一律放 `data/` 与 `logs/`（已 gitignore），不进版本库。

## 领域硬规则（写代码时必须落地为代码约束，而不是提示词祈求）

### 第一原则：无据不答

**禁止输出经验推断。** POE1 机制耦合极深，一个错误的小前提会让整条结论反向。查不到就明确输出缺据声明，不要给一个听起来合理的答案。输出错误内容的代价高于输出「查不到」。

具体禁止：无引用支撑的机制描述、凭记忆给出的数值、按同类物品类推的属性或价格、无权重数据支撑的制作成功率、猜测的词缀 T 级、未经证实的版本改动。也禁止用「一般来说」「通常」「大概」把推断包装成模糊表述。

按内容类型分级：

| 类型 | 规则 |
| --- | --- |
| 事实断言（机制/数值/词缀/版本） | 必须绑定引用或工具输出，无据即拦截并替换为缺据声明 |
| 计算派生（已引用事实 + 计算器输出） | 允许，必须附完整推导链且每个输入可溯源 |
| 建议（优化/制作/投资/规划） | 允许，必须标注为建议 + 写出假设，事实前提逐条带引用 |

拒答粒度是**单条断言**，不是整轮回答——有据的照答，无据的逐条列出缺什么、去哪查。缺据声明要写入缺口台账，用于驱动后续采集。

### 其余硬规则

1. **答案必须可溯源。** 任何事实断言都要带 `Citation`（来源 + 标题 + URL）。缺 `url` 或 `source` 的文档不得进入可引用索引；抽取失败的条目不得以部分字段入库。
2. **来源优先级内建于排序。** P1 POEDB > P2 Wiki > P3 Craft of Exile > P4 经济源 > P5 官方 Patch Notes > P6 社区讨论。P6 **不得**支撑事实断言，仅可标注为社区观点用于建议。
3. **断言—引用绑定校验是出口闸门。** 生成后逐句校验事实断言是否绑定有效证据，未通过者由代码拦截，不依赖模型自觉。流式输出必须按句缓冲，校验通过才下发——不能把未校验内容先推给用户。
4. **数值由工具产出，不由 LLM 心算。** 工具返回的数值需回填校验；模型改写数值时以工具结果为准并记录告警。
5. **Build 报告字段不得遗漏。** 需求 8.4 列出的检查项以 Pydantic 模型字段固化，缺项即校验失败；数据缺失标「未知」而非填估计值。
6. **PoB 的汇总数值直接读取，不重算。** 本项目不重新实现 PoB 伤害引擎。
7. **两个方向都要防。** 收紧规则会带来过度拒答。无据断言漏出率与错误拒答率必须成对监控，不能只压一头。

## 数据源现状（2026-08-04 实测，勿凭记忆改动）

- 当前 POE1 赛季为 **Allflame**（2026-07-24 开始）。赛季名从 GGG `api/leagues` 动态解析，**不要硬编码**。
- **poe.ninja 旧公开 API 已下线**（`/api/data/currencyoverview`、`itemoverview` 等全部 404）。不要写依赖它的代码。
- 经济数据主源用 `api.poe.watch`，官方 Trade API 作兜底，全部经 `EconomySource` 适配层接入。
- GGG `api/trade/data/{stats,items,static}` 免鉴权可用，适合做词缀与底材字典。
- 结构化游戏数据用 `PathOfBuildingCommunity/PathOfBuilding` 的 `src/Data/`。**不要用 RePoE**，它已于 2024-12 停更。

## 采集合规

只抓公开页面；遵守 robots.txt；令牌桶限速 + 磁盘缓存 + 内容哈希增量；携带可识别 User-Agent；输出中链回原站。不做任何游戏客户端注入、内存读取或自动交易。

## 安全

- `/index` 与 `/admin` 会触发外部抓取和索引重建，必须使用**独立管理凭证**，不与普通用户 API Key 共用；新增这类端点时同步加鉴权，不要留待后补。
- 普通 API 按 Key 限流，避免被当作免费 LLM 代理。
- 密钥只从环境变量读取；错误响应不得泄露密钥、内部路径或堆栈。

## 测试

- 计算器与解析器必须有单元测试，用真实样本作 fixture（真实 Ctrl+C 装备文本、真实 PoB 码）。
- 测试中外部数据源与 LLM 一律打桩，保证可离线运行。
- 涉及数值的改动，抽样与 POEDB 原文人工核对，不以「无报错」代替验证。

## 语言

面向用户的输出默认简体中文，关键术语保留英文原文（如「法术压制（Spell Suppression）」）。代码标识符与注释用英文，文档与提交信息用中文。
