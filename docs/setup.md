# 安装、模型配置与客户端接入

[← 项目首页](../README.md) · [文档导航](README.md)

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
#    首次完成唯一装备详情补全后，后续请用 docs/operations.md 中的完整刷新命令；
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

## PoB 数据导入

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
