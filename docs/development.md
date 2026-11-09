# 开发、评测与项目结构

[← 项目首页](../README.md) · [文档导航](README.md)

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

目前 2513 项单元测试覆盖以下与「无据不答」直接相关的重点：

| 组 | 重点 |
| --- | --- |
| 出口绑定闸门 | 事实数值溯源、计算器派生回填与成对的错误拒答/无据漏出压制 |
| 装备文本解析 | 真实 Ctrl+C 文本 fixture；「普通复制不许带 T 级」「认不出必须显式失败」 |
| 词缀 T 级匹配 | 内存桩库，数值照抄真实库；多行匹配、药剂专页、中英文固有隐式区间与歧义拒答 |
| 装备分析报告 | 「不给稀有装备估值」「容量输入不完整不报余量」「跨页档位一致性与物等门槛」 |
| 实时经济价格、成本与双边价差 | 精确名称、批量清单加总、计价单位、联盟、TTL、缓存优先降级、exchange 与唯一装备 search/fetch、多币种换算、双边方向、整单深度、挂单时效、显式费用、跨联盟/无效/离群过滤、最低样本门槛与 unknown 降级 |
| 游戏常量访问 | 缺失即抛异常，绝不给默认值 |
| 流式闸门一致性 | 流式与批量校验的判定必须完全一致 |
| 表格断言单位 | 表格整块算一个断言；绑定位置放宽但「要不要绑定」没放宽；表格未收齐不下发 |
| 维度覆盖闸门 | 实体在库里 ≠ 被问的维度有数据；复合问题不整轮拒答 |
| 缺口台账 | 排版元素不入账、归一化合并、三类来源分开 |
| 黄金问答集自检 | 评测集本身写错会给出「看起来正常的错数字」，比报错危险 |

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
