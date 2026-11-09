# 数据同步、备份与恢复

[← 项目首页](../README.md) · [文档导航](README.md)

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

## 备份与恢复

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
