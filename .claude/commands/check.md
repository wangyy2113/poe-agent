---
description: 收工前的全套体检：单测 + 自检 + spec 交叉引用
---

按顺序跑完这三项，逐项报结果，**有失败就停下来说清是哪一项**，不要继续往下做别的事：

```bash
.venv/bin/python -m unittest discover -s tests -t .
.venv/bin/python scripts/selfcheck.py
.venv/bin/python scripts/check_spec_refs.py
```

判读要点：

- 单测当前应有 340+ 项，**秒级跑完**。变慢意味着某个用例开始连库或连网了——
  装备相关测试全部离线是硬规则（解析器不连库，T 级匹配用内存 SQLite 打桩）。
- `selfcheck.py` 报「向量扩展可用但未配置维度」是**正常**的：`vector.enabled=false`，
  网关没有 embedding 模型。不要为了让这行消失去改配置。
- 单测数量比上次少了要当回事：可能是某个模块 import 失败导致整个文件被跳过，
  那种情况 unittest 会报 ERROR 而总数下降，别只看「OK」。
