# 装备文本 fixture

每个 `.txt` 是一份装备文本样本，来源逐个标注——**样本的可信度决定测试的可信度**，
所以这里不含来源不明的样本。

| 文件 | 来源 | 说明 |
| --- | --- | --- |
| `unique_nebulis_advanced_en.txt` | Path of Building 社区版 `spec/System/TestItemParse_spec.lua`（MIT） | 真实进阶复制（Ctrl+Alt+C）文本，含天灾/附魔/进阶词缀头/隐匿占位/影响词/熔炉段 |
| `rare_gloves_en.txt` | 按 PoB `src/Classes/Item.lua` 的解析分支构造的标准 Ctrl+C 文本 | 稀有手套，隐式 + 显式 + 工艺台 + 腐化 |
| `rare_bow_fractured_en.txt` | 同上 | 稀有弓，裂隙词缀 + 影响词 + 多组插槽 |
| `magic_ring_en.txt` | 同上 | 魔法戒指，只有一行名称（名称含词缀前后缀） |
| `rare_amulet_zh.txt` | 词缀文本逐字取自本项目 POEDB 词缀库（`mod_affix.text_zh`），数值取各 T 级区间中点 | 中文稀有项链。**表头标签待真机核实**，见下 |
| `garbage.txt` | 构造 | 不是装备文本，用于验证解析失败契约 |

## 中文样本的诚实说明

英文格式有 PoB 的解析实现作依据，可核实。**中文客户端 Ctrl+C 的表头标签没有可核实来源**：
POEDB 缓存页里只有词缀文本，没有物品提示框的标签行。所以 `rare_amulet_zh.txt` 的
`物品类别:` / `稀有度:` / `物品等级:` 这些键是推定的。

已做的兜底：解析器对未识别的 `键: 值` 一律原样收进 `properties` 并记 warning，
不按位置硬认，所以标签认错只会让该字段留空，不会静默错解析。

该样本刻意**不使用隐式词缀的语言后缀**（英文是 ` (implicit)`，中文标签未知），
改为依赖「隐式段在显式段之前」的位置推断——这条推断本身也需要被测到。

要提高可信度：拿一份真机中文客户端的 Ctrl+C 文本替换该文件，并据此校正
`parsers/item_text.py` 里的 `_ZH_KEYS` / `_ZH_FLAG_LINES`。
