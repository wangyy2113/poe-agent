"""断言—引用绑定校验：出口闸门（任务 3.6，需求 1.2/1.7/1.8/15.2/15.3/15.4）。

这是「无据不答」的最后一道防线，也是唯一由**代码**执行而非依赖模型自觉的一道。

做法：
  1. 进入生成的每条证据分配编号 `E1..En`，随内容一起进上下文。
  2. 提示词要求每个事实句末尾附 `[[E<n>]]`。
  3. 出口逐句校验：分句 → 分类 → 检查绑定 → 检查证据是否真正支撑该句。
  4. 未通过者**由代码拦截**并替换为该点的缺据声明。

为什么校验「证据是否真正支撑」而不只是「有没有标注」：模型完全可以随手写个
`[[E1]]` 蒙过去。所以对事实句里出现的**数值**做回填校验——数值必须能在它绑定的
证据原文里找到。这一条能抓住绝大多数编造，因为编造的具体来说就是数字。

三类内容分级（需求 15.2–15.4）：
  事实断言   强制绑定引用，未绑定即拦截
  计算派生   允许，但要有推导链且输入可溯源
  建议       允许，必须可辨识为建议
过渡句（「下面分几点说明」）不承载事实，放行。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

logger = logging.getLogger(__name__)

SentenceKind = Literal["factual", "derived", "recommendation", "narrative"]

#: 绑定标记。允许 `[[E1]]`、`[[E1,E3]]`、`[[E1][E3]]` 几种写法——
#: 模型的输出格式不会百分百听话，为格式差异拦掉有据的句子是「错误拒答」，
#: 与漏出同样要压。
#: 分隔符：逗号/分号（含全角），或模型自己拼出来的括号形式 `][`、`],[`、`], [`。
#: 括号形式必须显式列出——2026-08-18 实测 `[[E1][E3]]` 与 `[[E1],[E13]]` 都认不出来，
#: 而上面这段注释一直声称支持前者。**代码没做到自己声明的事**，后果是一句有据的话
#: 因为标记写法被判「未绑定引用」，属错误拒答。
_BINDING_SEP = r"(?:[,，;；]|\]\s*[,，]?\s*\[)"
#: 单层方括号 `[E1]` 也算绑定标记。理由与括号变体、正负号归一同一条：
#: **它是同一个意图的另一种写法**，不是更弱的证据要求——数值回填校验照旧跑。
#: 实测（第二十一轮，92 条录制离线重放）：144 句拦截里有 **8 句**只因为模型写了
#: `[E1]` 而不是 `[[E1]]`，涉及 7 条录制。提示词里写了双层写法也拦不住这件事，
#: 而拦下来的是一句**有据的话**——属错误拒答。
_BINDING_RE = re.compile(
    rf"\[{{1,2}}\s*(E\d+(?:\s*{_BINDING_SEP}\s*E\d+)*)\s*\]{{1,2}}", re.I
)
_LABEL_RE = re.compile(r"E(\d+)", re.I)

#: 分句。中文标点 + 换行。**不切小数点**（「3.5 秒」不能被拆开），
#: 也不切绑定标记内部——`[[E1;E2]]` 里的分号是标记的一部分，
#: 切开会让标记失效、有据的句子被误拦（错误拒答）。
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*|\n+")

#: Markdown 结构行：标题、加粗标签、水平分隔线、空列表符号。
#: 这些是**排版**而非断言。早先按事实句处理，实测一次回答 16 句里拦了 5 句，
#: 全是 `## 猎首（Headhunter）`、`**隐式词缀**`、`---` 这类——答案结构被撕碎，
#: 属严重的错误拒答。
#: 但带数字的标题仍走事实句校验：数值是编造的主要载体，
#: `## 生命上限 1000` 这种要拦。
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", re.S)
_MD_RULE_RE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_MD_LABEL_RE = re.compile(r"^\s*(?:[-*+]\s+)?\*{1,3}([^*\n]{1,40})\*{1,3}\s*[：:]?\s*$")
_MD_BULLET_ONLY_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s*$")

#: Markdown 表格行与分隔行。
#: 表格必须整块当一个断言单位看，见 `_merge_table_blocks` 的说明。
_MD_TABLE_ROW_RE = re.compile(r"^\s{0,3}\|.*\|\s*$")
_MD_TABLE_SEP_RE = re.compile(r"^\s{0,3}\|(?:\s*:?-{2,}:?\s*\|)+\s*$")

#: 纯过渡/元话语句：只描述回答本身的组织，不承载游戏事实。
#: 这是**白名单**——判不出来的一律按事实句处理（见 `classify`）。
#: 锚定在句首并限制长度，避免「另外，猎首还会让你免疫冰冻」这类
#: 以连接词开头的真事实断言蒙混过关。
_NARRATIVE_RE = re.compile(
    r"^[\s，,、]*(?:下面|以下|接下来|下文|综上|综合以上|总结|小结|先说结论"
    r"|需要说明的?是|说明如下|分别说明|逐条说明|简要说明|回答如下)"
    r"[^。！？\n]{0,12}[。！？：:\s]*$"
)

#: 冒号收尾的引导句：`该核心节点具有以下效果：`、`显式词缀如下：`、`按来源渠道分别列出：`。
#:
#: 单独一条规则而不是放宽 `_NARRATIVE_RE`：那个正则锚定句首，而这类句子的引导词
#: 常在**句中**（「具有以下效果」）。而单纯放宽前缀等于允许标记之前出现任意内容，
#: 「它会免疫冰冻，效果如下：」就会跟着漏出去——那正是 classify 默认判事实句要防的。
#:
#: 三道结构性限制把风险收窄，缺一不可：
#:   **以引导词 + 冒号收尾**  冒号意味着内容在后面，而后面的句子各自过闸门；断言不以冒号结尾
#:   **整句不含逗号**        逗号意味着有子句，也就是可能夹带断言
#:                           （「猎首会偷取词缀，说明如下：」因此不放行）
#:   **不含任何数值**        数值是编造的主要载体，含数值一律回事实句校验（在 classify 里判）
_COLON_LEAD_IN_RE = re.compile(
    r"^[^。！？，,、\n]{0,20}?"
    r"(?:如下|以下|下列|分别是|列出|包括)"
    r"[^。！？，,、\n]{0,8}[：:]\s*$"
)

#: 引导句的第二形态：**带证据框架前缀的长引导句**。
#:
#: 48 句人工审计里 14 条错误拒答，7 条是这一类，是剩余错误拒答的最大单一来源：
#:
#:   根据所提供的证据，符合「击败稀有怪物时获得其属性修正」机制的腰带为：
#:   根据证据，不同胸甲底材类别上的 MaximumResistances（抗性上限）词缀如下：
#:   以下证据中出现的物品与该机制相关，但**不符合**「腰带」这一限定：
#:
#: 它们过不了 `_COLON_LEAD_IN_RE` 的两道限制：句中有逗号、引导词后超过 8 字。
#: 但那两道限制是「有没有夹带断言」的**代理判据**，不是判据本身。直接放宽它们会让
#: 上面注释里那个反例（「猎首会偷取词缀，说明如下：」）漏出去，所以这里换一组更贴近
#: 语义的合取条件，四条缺一不可：
#:
#:   **框架前缀**    句首是「根据/依据/基于/以下/需注意…」这类**指向证据或指向下文**的词。
#:                   反例「猎首会偷取词缀，说明如下：」句首是实体名，命不中。
#:   **前指词收尾**  冒号前必须落在封闭词集上（如下/信息/清单/为/是/限定…），
#:                   即这句的落点是「下面的内容」而不是一个谓语。
#:                   「根据证据，猎首会偷取稀有怪物的词缀：」落在「词缀」上，仍然拦。
#:   **无数值**      与第一形态同一条（在 `classify` 里判）。
#:   **长度上限**    冒号收尾的长段落本身可疑。
#:
#: 放行引导句**不会**顺带放行它引出的内容：后面的句子、表格各自过闸门。表格继承只继承
#: 「已通过且带标记」的前一句，而引导句被判为 narrative、不带标记，继承不到任何东西。
_FRAMED_LEAD_IN_RE = re.compile(
    r"^[\s*＊>]*"                                   # 可能残留的加粗/引用标记
    r"(?:根据|依据|基于|以下|下列|需注意|请注意|注意|其中|另外|此外|结合)"
    r"[^。！？\n]{0,60}?"
    r"(?:如下|以下|下列|列出|包括|分别是|信息|清单|结果|内容|限定|组合|为|是)"
    r"[\s]*[：:]\s*$"
)

#: 数值。带千分位与小数，含正负号与百分号。
_NUM_RE = re.compile(r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?")

#: 排版用的正负号 → ASCII。**只映射真正的正负号**：
#: `−`(U+2212 减号)、`＋`/`－`(全角)。连接号 `–`/`—`/`~` 一律不动——
#: 在这个领域里它们是区间分隔符（`(40–55)`），当成负号会把区间读成两个负数。
#:
#: 为什么必须归一（2026-08-19 第二十轮实测）：模型写「抗性下限为 −200%」，
#: 而证据原文写 `-200%`。提取器认不出 U+2212，于是从模型那句里读出 `200`、
#: 在证据里找不到 `200`（只有 `-200`），把一句**完全正确**的话判成编造。
#: 抗性、减抗、诅咒这些场景全是负数，不归一等于对整类负数误拦。
_SIGN_NORMALIZE = str.maketrans({"\u2212": "-", "\uff0d": "-", "\uff0b": "+"})


def normalize_signs(text: str) -> str:
    """把排版正负号换成 ASCII，供数值提取用。"""
    return text.translate(_SIGN_NORMALIZE)

#: 建议类句式。命中即归为建议，不强制绑定，但必须能被用户辨识为建议。
_RECOMMEND_HINTS = (
    "建议", "推荐", "可以考虑", "不妨", "更划算", "更稳", "优先选", "思路是",
    "如果预算", "值得", "取舍", "我倾向", "一般做法",
)
#: 推导标记。命中且句中数值均可溯源即视为计算派生。
_DERIVED_HINTS = ("因此", "所以", "计算得", "推导", "合计", "= ", "＝", "换算")

#: Markdown 强调标记。只在缺据声明判定里剥掉——那条规则匹配的是**构式**，
#: 而 `**没有任何一条**直接陈述` 的星号正好落在构式中间，把构式切断了。
#: 不全局剥：`_MD_LABEL_RE` 这些规则**依赖** `**` 来认加粗标签。
_EMPHASIS_RE = re.compile(r"[*＊_]{1,3}")

#: 缺据声明句式：陈述「证据里没有某项信息」。
#: 这类句子断言的是信息的**缺失**，本质上无法绑定证据——要求它带引用是自相矛盾的。
#: 实测模型主动声明缺据时被闸门拦掉了，而那正是「无据不答」希望它做的事。
#:
#: 用**正则**而非关键词表：实测两轮各撞一次同类假阳性，只是措辞不同
#: （「未包含」→「不包含」、「本次证据」→「本轮证据」）。枚举词表补不完，
#: 按「否定词 + 提供类动词」的构式匹配才收得住。
#: 第三轮补的两类构式（2026-08-17 人工审计实测，两条都属错误拒答）：
#:   「根据结构化词缀库的直接查询…**均返回无匹配结果**」  查询结果为空的声明
#:   「证据 [E2]–[E8] 全部为…**与技能石数值无关**」        证据与问题不相关的声明
#: 两者与既有构式是同一件事——断言的是**信息的缺失或不相关**，本质上无法绑定证据，
#: 要求它带引用是自相矛盾的。
#: 第五轮补的一类（48 句审计实测，判为错误拒答）：
#:   「所提供的 16 条证据中，**没有任何一条**直接陈述"物理伤害减免上限为 X%"。」
#: 两处漏：`陈述` 不在动词表里；量词与动词之间夹着副词（`直接`）。另外句中的 `**`
#: 会把构式切断，所以匹配前先去掉强调标记（只在这条判定里去，不影响 `_MD_LABEL_RE`
#: 那些**依赖** `**` 的规则）。
_DISCLOSURE_RE = re.compile(
    # 否定词与动词之间允许一个量词：「**没有一条**涉及偷取」实测被拦（错误拒答）
    # 量词是**两格**：「没有任何一条」= 否定 + 任何 + 一条。写成单格 alternation 时
    # 「任何」吃掉后剩下的「一条」会把动词挡住，整条构式失配（实测）。
    r"(?:未|不|没(?:有)?|无)\s*(?:任何|任一|每)?\s*(?:一条|一项|一个|一处|哪一?条)?\s*"
    r"(?:直接|明确|清楚|逐字|具体)?\s*"
    r"(?:包含|涉及|提供|给出|收录|说明|记载|列出|陈述|写明|表明|提及|标注)"
    r"|(?:本次|本轮|当前|以上|上述|该批)\s*证据"
    r"|证据(?:中|里)?(?:没有|不含|未)"
    r"|(?:知识库|库)(?:中|里)?(?:没有|查不到|未收录)"
    r"|(?:无|未)(?:匹配|命中|检索到|查到|返回)(?:结果|结论|记录|条目|数据)?"
    r"|(?:返回|查询|检索)(?:结果)?为空"
    r"|与[^。！？\n]{0,20}?(?:无关|不相关)"
    r"|查不到|无法确认|缺少依据|需(?:另行|进一步)查证"
)

#: 面向用户的操作指引：「请单独提问」「如需…请…」。
#: 这类句子讲的是怎么继续用这个助手，不是在断言游戏事实。
#: 要求**不含数字**——带数字的「如需满抗请堆到 75%」是建议或事实，不是操作指引。
_META_INSTRUCTION_RE = re.compile(
    r"(?:请(?:单独|另行|重新)?(?:提问|询问|告知|补充|指定|说明))"
    r"|(?:如(?:果)?需|若需|需要).{0,16}(?:请|可以)"
    r"|(?:可以再(?:问|提问))|(?:欢迎(?:继续)?提问)"
)

#: 模糊限定语。需求 1.5/15.1 明令禁止用它们把推断包装成模糊表述。
#: 这类词出现在事实句里，即使绑了引用也要拦——它们的作用就是回避确定性。
_HEDGE_WORDS = (
    "一般来说", "通常来说", "通常情况下", "大概是", "大约是", "应该是", "可能是",
    "据我所知", "印象中", "记得是", "差不多是", "多半",
)

#: 工艺台目录 scope 使用“结构化字段正向匹配”，而不是靠扩充同义词黑名单。
#: 下列模式只用于生成可审计的拦截原因；未命中任何模式也会按“目录未覆盖”拒绝。
_BENCH_CATALOG_FORBIDDEN: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("给定装备状态下的可执行性", re.compile(
        r"可执行|执行该工艺|用于当前装备|当前装备.{0,8}(?:使用|制作|应用)"
        r"|(?:能|可以|可).{0,12}(?:使用|制作|应用)"
    )),
    ("装备状态转移或词缀替换/移除", re.compile(
        r"装备状态|状态转移|(?:添加|删除|移除|替换|覆盖|保留).{0,10}(?:词缀|属性|前缀|后缀|crafted[ -]?mod)"
    , re.I)),
    ("制作步骤或操作顺序", re.compile(
        r"制作步骤|操作步骤|操作顺序|制作流程|第一步|第二步|下一步|先.{0,20}再|依次"
    )),
    ("成功率或概率", re.compile(r"成功率|成功机会|成功可能|概率|几率|机率|期望次数|平均次数")),
    ("市场成本或价格", re.compile(r"市场成本|实际成本|市价|市场价|行情|折算价格|交易价格")),
    ("最快、最经济或最优路线", re.compile(
        r"最快|最经济|最省|最便宜|最优|路线|方案比较|成本最低"
    )),
    ("词缀容量、crafted-mod 上限或元词缀交互", re.compile(
        r"词缀容量|crafted[ -]?mod.{0,6}上限|工艺词缀.{0,6}上限|元词缀.{0,8}(?:交互|作用|影响)"
    , re.I)),
)
_BENCH_PURE_BOUNDARY = re.compile(
    r"^(?:该)?目录(?:记录)?(?:不|无法|不能|不足以)证明(?:它)?(?:在)?"
    r"(?:任意|当前|给定|这件|该)?(?:给定)?装备状态下可执行$"
)
_BENCH_EFFECT_CLAIM = re.compile(
    r"^\s*(?:poedb\s*)?(?:页面|目录|页面目录)?\s*(?:列出的?\s*)?"
    r"效果原文\s*(?:是|为|：|:)\s*(.+?)\s*[。！？!?]*$",
    re.I,
)
_BENCH_COST_CLAIM = re.compile(
    r"^\s*(?:poedb\s*)?(?:页面|目录|页面目录)?\s*(?:列出的?\s*)?"
    r"(?:原始费用|页面费用)\s*(?:是|为|：|:)\s*(.+?)\s*[。！？!?]*$",
    re.I,
)
_BENCH_ITEM_CLASSES_CLAIM = re.compile(
    r"^\s*(?:poedb\s*)?(?:页面|目录|页面目录)?\s*(?:列出的?\s*)?"
    r"(?:适用\s*itemclasses|适用类别)\s*(?:是|为|：|:)\s*(.+?)\s*[。！？!?]*$",
    re.I,
)
_BENCH_UNLOCK_CLAIM = re.compile(
    r"^\s*(?:poedb\s*)?(?:页面|目录|页面目录)?\s*(?:列出的?\s*)?"
    r"解锁(?:信息|文本|说明)?\s*(?:是|为|：|:)\s*(.+?)\s*[。！？!?]*$",
    re.I,
)
_BENCH_COST_PAIR = re.compile(
    r"([-+]?\d+(?:\.\d+)?)\s*[xX×]\s*(.+?)(?="
    r"\s*(?:\+|和|与|、|，|,)\s*[-+]?\d+(?:\.\d+)?\s*[xX×]|\s*$)"
)
_BENCH_CATALOG_PRESENCE = re.compile(
    r"^\s*(?:poedb\s*)?(?:页面)?目录(?:中|里)?\s*"
    r"(?:列出|收录|有|存在)了?\s*(?:该选项|这个选项|该目录项)\s*[。！？!?]*$",
    re.I,
)

#: Metamod Matrix v1 只允许三种正向字段声明；其它绑定语义全部 fail closed。
_METAMOD_RESULT_CLAIM = re.compile(
    r"^\s*来源矩阵记录[“\"](.+?)[”\"]在[“\"](.+?)[”\"]下的"
    r"原始结果(?:是|为|：|:)\s*(YES\s*&\s*NO|YES|NO|N/A)\s*[。！？!?]*$",
    re.I,
)
_METAMOD_NOTE_CLAIM = re.compile(
    r"^\s*来源矩阵对[“\"](.+?)[”\"]与[“\"](.+?)[”\"]的"
    r"原始注释(?:是|为|：|:)\s*(.+?)\s*[。！？!?]*$",
    re.I,
)
_METAMOD_PRESENCE_CLAIM = re.compile(
    r"^\s*来源矩阵包含[“\"](.+?)[”\"]与[“\"](.+?)[”\"]的交互记录\s*[。！？!?]*$",
    re.I,
)
_METAMOD_PURE_BOUNDARY = re.compile(
    r"^\s*(?:该)?矩阵记录(?:不|无法|不能|不足以)证明(?:它)?(?:在)?"
    r"(?:任意|当前|给定|这件|该)?装备状态下可执行\s*[。！？!?]*$"
)
_METAMOD_FORBIDDEN: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("给定装备状态下的可执行性", re.compile(
        r"可执行|执行该操作|用于当前装备|当前装备.{0,8}(?:使用|制作|应用)"
        r"|(?:能|可以|可).{0,12}(?:使用|制作|应用)"
    )),
    ("完整状态转移或结果外推", re.compile(
        r"状态转移|(?:添加|删除|移除|替换|覆盖|保留).{0,10}(?:词缀|属性|前缀|后缀)"
    )),
    ("制作步骤或操作顺序", re.compile(
        r"制作步骤|操作步骤|操作顺序|制作流程|第一步|第二步|下一步|先.{0,20}再|依次"
    )),
    ("成功率或概率", re.compile(r"成功率|成功机会|成功可能|概率|几率|机率|期望次数|平均次数")),
    ("市场成本或价格", re.compile(r"市场成本|实际成本|市价|市场价|行情|折算价格|交易价格")),
    ("最快、最经济或最优路线", re.compile(r"最快|最经济|最省|最便宜|最优|路线|方案比较|成本最低")),
    ("当前版本或赛季适用性", re.compile(r"当前版本|当前赛季|本赛季|现版本|3[.]\d+")),
)

#: Affix Pool v1 只允许底材映射、完整池整数权重、条件分数与独立选择数学期望。
_AFFIX_POOL_BASE_CLAIM = re.compile(
    r'^\s*底材[“"](.+?)[”"]（(.+?)）映射到\s*POEDB\s*词缀页[“"](.+?)[”"]\s*[。！？!?]*$',
    re.I,
)
_AFFIX_POOL_WEIGHT_CLAIM = re.compile(
    r'^\s*在\s*normal\s*来源、物品等级\s*(\d+)\s*、(前缀|后缀)侧且未应用'
    r'已有词缀\s*family\s*排除的条件下，有效池总权重为\s*(\d+)\s*，'
    r'目标词缀组[“"](.+?)[”"]权重为\s*(\d+)\s*，条件概率为\s*'
    r'(\d+)\s*/\s*(\d+)\s*（([0-9]+(?:[.]\d+)?)）\s*[。！？!?]*$',
    re.I,
)
_AFFIX_POOL_EXPECTED_CLAIM = re.compile(
    r'^\s*在相同条件的独立同分布选择模型中，命中目标词缀组[“"](.+?)[”"]的'
    r'数学期望选择次数为\s*(\d+(?:/\d+)?)\s*[。！？!?]*$',
    re.I,
)
_AFFIX_POOL_PURE_BOUNDARY = re.compile(
    r'^\s*(?:该)?条件概率不是任何通货或具体工艺的一次使用成功率\s*[。！？!?]*$'
)
_AFFIX_POOL_FORBIDDEN: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("具体通货或工艺成功率", re.compile(
        r"(?:改造石|增幅石|富豪石|混沌石|精华|化石|收获|harvest|metamod|工艺).{0,12}(?:成功率|概率|几率|机率)",
        re.I,
    )),
    ("实际期望通货消耗", re.compile(r"期望通货|平均需要.{0,12}(?:石|精华|化石|次工艺)|预计消耗")),
    ("当前装备状态、容量或可执行性", re.compile(
        r"当前装备|已有词缀|剩余(?:前缀|后缀|位置)|词缀容量|可执行|可以制作|能够制作"
    )),
    ("特殊来源或影响力外推", re.compile(
        r"影响力|裂界|塑界|精华|化石|收获|harvest|隐匿|异能|synthesis|metamod",
        re.I,
    )),
    ("制作步骤或操作顺序", re.compile(
        r"制作步骤|操作步骤|操作顺序|制作流程|第一步|第二步|下一步|先.{0,20}再|依次"
    )),
    ("市场成本或价格", re.compile(r"市场成本|实际成本|市价|市场价|行情|折算价格|交易价格")),
    ("最快、最经济或最优路线", re.compile(r"最快|最经济|最省|最便宜|最优|路线|方案比较|成本最低")),
)


@dataclass(frozen=True)
class Evidence:
    """进入生成的一条证据。`label` 是注入上下文的编号。"""

    label: str
    chunk_id: str
    entity_id: str | None
    title: str
    url: str
    source: str
    source_priority: int
    text: str
    can_support_facts: bool = True
    #: 证据可支撑的事实范围。`general` 保持既有证据行为；`bench_catalog`
    #: 只能支撑页面目录字段，不能被反向用于证明制作执行语义。
    claim_scope: str = "general"
    #: 受限证据的结构化原字段。范围闸门只按这里的值做正向匹配，不能仅靠关键词。
    claim_data: dict[str, Any] = field(default_factory=dict)

    @property
    def numbers(self) -> set[str]:
        """证据里出现的全部数值，归一后用于回填校验。"""
        return {
            _norm_num(m.group())
            for m in _NUM_RE.finditer(normalize_signs(self.text))
        }


@dataclass
class SentenceVerdict:
    text: str
    kind: SentenceKind
    labels: tuple[str, ...]
    ok: bool
    reason: str
    #: 未通过时代替原句下发的内容
    replacement: str = ""
    #: 未通过时记入缺口台账的缺失信息点
    missing_point: str = ""

    @property
    def output(self) -> str:
        return self.text if self.ok else self.replacement


@dataclass
class BindingResult:
    verdicts: list[SentenceVerdict] = field(default_factory=list)
    #: 被拦截的事实句数，对应指标 leakage_blocked
    blocked: int = 0
    #: 需要写入缺口台账的缺失点
    gaps: list[str] = field(default_factory=list)
    #: 实际被引用到的证据编号，用于底部来源列表（未被引用的不列）
    used_labels: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(v.output for v in self.verdicts)

    @property
    def all_passed(self) -> bool:
        return self.blocked == 0

    def summary(self) -> dict[str, Any]:
        """进轨迹的结构化结果（需求 4.5：拦截原因可追溯）。"""
        return {
            "sentences": len(self.verdicts),
            "blocked": self.blocked,
            "by_kind": {
                k: sum(1 for v in self.verdicts if v.kind == k)
                for k in ("factual", "derived", "recommendation", "narrative")
            },
            "blocked_detail": [
                {"text": v.text[:120], "reason": v.reason}
                for v in self.verdicts if not v.ok
            ],
            "used_labels": self.used_labels,
        }


# ── 证据编号 ───────────────────────────────────────────────

def number_evidence(citations: Iterable[dict[str, Any]]) -> list[Evidence]:
    """给证据分配 `E1..En`。同一 chunk 只编一次，避免重复占号。"""
    out: list[Evidence] = []
    seen: set[str] = set()
    for c in citations:
        cid = str(c.get("chunk_id") or "")
        if cid and cid in seen:
            continue
        seen.add(cid)
        priority = int(c.get("source_priority") or 99)
        out.append(
            Evidence(
                label=f"E{len(out) + 1}",
                chunk_id=cid,
                entity_id=c.get("entity_id"),
                title=str(c.get("title") or ""),
                url=str(c.get("url") or ""),
                source=str(c.get("source") or ""),
                source_priority=priority,
                text=str(c.get("text") or ""),
                can_support_facts=bool(c.get("can_support_facts", True)),
                claim_scope=str(c.get("claim_scope") or "general"),
                claim_data=(
                    dict(c["claim_data"])
                    if isinstance(c.get("claim_data"), dict)
                    else {}
                ),
            )
        )
    return out


def render_evidence_block(evidence: list[Evidence]) -> str:
    """渲染进提示词的证据块。**不含 URL**——引用格式由代码在出口装配，
    给了 URL 模型就会自己编引用。"""
    parts = []
    for e in evidence:
        tags: list[str] = []
        if not e.can_support_facts:
            tags.append("社区来源，不可支撑事实断言")
        if e.claim_scope == "bench_catalog":
            tags.append("仅可支撑目录存在、效果原文、原始费用、ItemClasses 与解锁信息")
        if e.claim_scope == "metamod_matrix":
            tags.append("仅可支撑逐字 operation × guard 的原始结果与原始注释")
        if e.claim_scope == "affix_pool_odds":
            tags.append(
                "仅可支撑具体胸甲底材映射、normal 同侧完整池权重、条件概率与独立选择数学期望"
            )
        tag = f"（{'；'.join(tags)}）" if tags else ""
        parts.append(f"[{e.label}] 来源：{e.source} → {e.title}{tag}\n{e.text}")
    return "\n\n".join(parts)


# ── 校验 ───────────────────────────────────────────────────

def verify(
    answer: str,
    evidence: list[Evidence],
    *,
    factual_terms: Iterable[str] = (),
    domain_terms: Iterable[str] = (),
) -> BindingResult:
    """逐句校验并拦截。返回的 `text` 是可安全下发的内容。

    `factual_terms` 来自 config.yaml 的领域词表；`domain_terms` 来自库里的
    实体名与底材类别（`retrieval/vocab.py` 生成）。两者都用于事实句判定——
    命中即要求绑定引用。
    """
    by_label = {e.label.upper(): e for e in evidence}
    terms = tuple(t for t in factual_terms if t)
    domain = tuple(t for t in domain_terms if t)
    result = BindingResult()
    used: list[str] = []

    previous: SentenceVerdict | None = None
    for raw in split_sentences(answer):
        if not raw.strip():
            result.verdicts.append(
                SentenceVerdict(raw, "narrative", (), True, "空白")
            )
            continue
        # 表格块可继承紧邻前一句的绑定（见 `inheritable_labels`）。
        # 空白片段跳过不算「紧邻」被打断——`档位如下[[E1]]：\n\n| 表 |` 中间那个空行
        # 是排版，不该让表格失去可继承的上文。
        verdict = _check_sentence(
            raw, by_label, terms, domain,
            inherited=inheritable_labels(previous, raw),
        )
        previous = verdict
        result.verdicts.append(verdict)
        if verdict.ok:
            for lb in verdict.labels:
                if lb not in used:
                    used.append(lb)
        else:
            result.blocked += 1
            if verdict.missing_point:
                result.gaps.append(verdict.missing_point)

    # 第二遍：派生句必须有「已通过校验的事实句」作为推导输入。
    # 否则模型只要在编造的数值前加个「因此」就能绕过整道闸门。
    has_grounded_input = any(
        v.ok and v.kind == "factual" and v.labels for v in result.verdicts
    )
    if not has_grounded_input:
        for i, v in enumerate(result.verdicts):
            if v.kind != "derived" or not v.ok or "未溯源数值" not in v.reason:
                continue
            blocked = _block(
                v.text, v.kind, v.labels,
                "计算派生句的数值无法溯源，且整个回答中没有可作为推导输入的有据事实句",
                f"推导链缺少可溯源的输入：{_excerpt(_BINDING_RE.sub('', v.text))}",
            )
            result.verdicts[i] = blocked
            result.blocked += 1
            result.gaps.append(blocked.missing_point)

    result.used_labels = [
        lb for v in result.verdicts if v.ok for lb in v.labels
    ]
    # 去重保序
    seen_lb: set[str] = set()
    result.used_labels = [
        lb for lb in result.used_labels
        if not (lb in seen_lb or seen_lb.add(lb))  # type: ignore[func-returns-value]
    ]
    _enforce_affix_pool_complete_answer(result, by_label)
    return result


class StreamingGate:
    """增量版出口闸门：喂 token 分片，产出**已校验通过**的整句。

    为什么流式不能逐字下发：校验单位是**句子**——要判断一句是不是事实断言、
    绑定的编号是否有效、句中数值能否溯源，都得等这句说完。逐字推送等于把
    未校验内容先给了用户，之后再撤回；而撤回是做不到的，用户已经读到了。

    所以流式的粒度必然是句。前端可以对已通过的句子做打字机效果，
    观感接近逐字流，但**不会显示任何未校验内容**。

    与批量 `verify()` 必须行为一致：这里复用同一套分句与校验函数，
    不另写一份规则。两份规则迟早分叉，那时流式与非流式的结果就不一样了。
    """

    def __init__(
        self,
        evidence: list[Evidence],
        *,
        factual_terms: Iterable[str] = (),
        domain_terms: Iterable[str] = (),
    ) -> None:
        self._by_label = {e.label.upper(): e for e in evidence}
        self._terms = tuple(t for t in factual_terms if t)
        self._domain = tuple(t for t in domain_terms if t)
        self._buffer = ""
        self.verdicts: list[SentenceVerdict] = []
        self.blocked = 0
        self.gaps: list[str] = []

    # ── 证据 ──

    def add_evidence(self, evidence: list[Evidence]) -> None:
        """补进新检索到的证据。

        编号是**追加式**的（`_merge_evidence` 不重编已有 chunk），所以这里只需
        把新编号补进映射。若重编号，模型先前标的 `[[E1]]` 就会指向别的东西，
        已下发的句子也无法追回。
        """
        for e in evidence:
            self._by_label.setdefault(e.label.upper(), e)

    # ── 消费 ──

    def feed(self, delta: str) -> list[SentenceVerdict]:
        """喂一个分片；条件池精确模式会整轮缓冲到 flush 后再统一放行。"""
        if not delta:
            return []
        self._buffer += delta
        out = self._drain(final=False)
        if any(e.claim_scope == "affix_pool_odds" for e in self._by_label.values()):
            return []
        return out

    def flush(self) -> list[SentenceVerdict]:
        """收尾并执行全回答证据完整性约束。"""
        out = self._drain(final=True)
        if not any(e.claim_scope == "affix_pool_odds" for e in self._by_label.values()):
            return out
        result = self.result()
        self.verdicts = list(result.verdicts)
        self.blocked = result.blocked
        self.gaps = list(result.gaps)
        return list(self.verdicts)

    # ── 内部 ──

    def _drain(self, *, final: bool) -> list[SentenceVerdict]:
        out: list[SentenceVerdict] = []
        while True:
            piece, rest = self._take_sentence(final=final)
            if piece is None:
                break
            self._buffer = rest
            # 与批量同一套继承判据：上一条**非空**判定即「紧邻前一句」
            previous = next(
                (v for v in reversed(self.verdicts) if v.text.strip()), None
            )
            verdict = _check_sentence(
                piece, self._by_label, self._terms, self._domain,
                inherited=inheritable_labels(previous, piece),
            )
            self.verdicts.append(verdict)
            if not verdict.ok:
                self.blocked += 1
                if verdict.missing_point:
                    self.gaps.append(verdict.missing_point)
            out.append(verdict)
        return out

    def _take_sentence(self, *, final: bool) -> tuple[str | None, str]:
        """从缓冲区切出一个完整句子。切不出来返回 (None, 原缓冲)。"""
        buf = self._buffer
        if not buf:
            return None, buf

        # 绑定标记未闭合时**绝不切**：`[[E1;E2]]` 只写到 `[[E1;` 时，
        # 那个分号会被当成句末标点，切出来的半个标记会让有据句被判成未绑定。
        if _has_open_marker(buf):
            return (buf, "") if final else (None, buf)

        parts = split_sentences(buf)
        if not parts:
            return None, buf
        if final:
            # 收尾时全部交出。多于一句就先给第一句，其余留待下一轮循环。
            return parts[0], "".join(parts[1:])
        # 表格整块才是一个断言单位（见 `_merge_table_blocks`），所以**表格没结束
        # 就不能下发**：提前放行会让流式按行校验、批量按整块校验，判定就分叉了。
        #
        # 两个条件都要判：
        #   只剩表格一段            —— 后面可能还有行，等
        #   缓冲区最后一行还在表内  —— 下一行只写到 `|` 时 split 会切出两段，
        #                              于是上一行被提前放行。实测就是这样把
        #                              流式与批量的一致率从 32/32 打到 18/32 的。
        tail_line = buf.rsplit("\n", 1)[-1].lstrip()
        if _is_table_line(parts[0]) and (len(parts) == 1 or tail_line.startswith("|")):
            return None, buf
        if len(parts) == 1 and not _ends_sentence(buf):
            # 只有一段且没说完，继续等
            return None, buf
        if len(parts) == 1:
            return parts[0], ""
        return parts[0], "".join(parts[1:])

    # ── 结果 ──

    def result(self) -> BindingResult:
        """汇总成与批量校验同构的结果，供轨迹落库与引用装配复用。"""
        res = BindingResult(verdicts=list(self.verdicts), blocked=self.blocked,
                            gaps=list(self.gaps))
        seen: set[str] = set()
        used: list[str] = []
        for v in res.verdicts:
            if not v.ok:
                continue
            for lb in v.labels:
                if lb not in seen:
                    seen.add(lb)
                    used.append(lb)
        res.used_labels = used
        _enforce_affix_pool_complete_answer(res, self._by_label)
        return res


def _has_open_marker(text: str) -> bool:
    """缓冲区末尾是否有未闭合的 `[[`。"""
    open_at = text.rfind("[[")
    if open_at < 0:
        return False
    return text.find("]]", open_at) < 0


_SENT_END_CHARS = "。！？!?；;\n"


def _ends_sentence(text: str) -> bool:
    return bool(text) and text.rstrip()[-1:] in _SENT_END_CHARS or text.endswith("\n")


def split_sentences(text: str) -> list[str]:
    """分句，保留原有标点与空白，拼回去应与原文等价（除拦截替换）。

    保序保空白是有意的：出口要按句缓冲下发，切一次再拼一次不能改动内容，
    否则用户看到的文本与校验过的文本就不是同一份了。

    绑定标记内部不切（实测踩到）：`[[E1;E2]]` 里的 `;` 会被当成句末标点，
    切开后标记失效，一条有据的句子被判成未绑定引用——这是错误拒答。
    """
    if not text:
        return []
    protected = [(m.start(), m.end()) for m in _BINDING_RE.finditer(text)]

    def inside_marker(idx: int) -> bool:
        return any(lo < idx < hi for lo, hi in protected)

    out: list[str] = []
    pos = 0
    for m in _SENT_SPLIT_RE.finditer(text):
        end = m.end()
        if inside_marker(m.start()) or inside_marker(end):
            continue
        piece = text[pos:end]
        if piece:
            out.append(piece)
        pos = end
    if pos < len(text):
        out.append(text[pos:])
    return _merge_table_blocks(out)


def _is_table_line(piece: str) -> bool:
    """这个片段是否以表格行开头（含已合并的多行表格块）。

    只看**首行**：`_MD_TABLE_ROW_RE` 里的 `.` 不匹配换行，直接拿整块去 match 时
    合并后的表格块必然返回 False。踩过这个坑——流式因此照旧按行放行，
    与批量的判定一致率停在 18/32 不动。
    """
    first = piece.strip("\n").split("\n", 1)[0]
    # 先剥绑定标记：行末的 `|[[E1]]` 会让 `\|\s*$` 匹配不上，
    # 于是**带引用的表格行反而认不出是表格**——正好是最该放行的那一类。
    return bool(_MD_TABLE_ROW_RE.match(_BINDING_RE.sub("", first).strip()))


def _merge_table_blocks(pieces: list[str]) -> list[str]:
    """把连续的表格行并成**一个**断言单位。

    为什么必须整块看：分句按行切，于是一张表被撕成表头、分隔行和若干数据行。
    实测（32 条真实回答）192 句拦截里 **92 句（48%）是表格行**，两种形态都是错误拒答：

      `| T 级 | 词缀文本 | 物品等级 |`        表头不承载断言，却被判「事实句未绑定引用」
      `| T1/1 | …… | 60 | 3000 |`             模型把 `[[E1]]` 标在表格前后而不是每一行，
                                              于是每行都被判未绑定

    而表格的断言单位本来就是整张表：它的每个数值都由表头那一列的含义决定，
    单看一行既判不出类型也校不准数值。整块看之后，绑定标记出现在表内任何位置
    （表头、某一行、末行）都对整张表生效，数值仍然逐个回填校验——**该校的没放过**。

    拼回去与原文等价（只是把相邻片段连起来），所以下发内容不变。
    """
    if not pieces:
        return pieces
    out: list[str] = []
    buf: list[str] = []
    for piece in pieces:
        if _is_table_line(piece):
            buf.append(piece)
            continue
        if buf:
            out.append("".join(buf))
            buf = []
        out.append(piece)
    if buf:
        out.append("".join(buf))
    return out


def _table_rows(block: str) -> list[list[str]]:
    """表格块 → 数据行的单元格矩阵（跳过分隔行）。"""
    rows: list[list[str]] = []
    for line in block.splitlines():
        s = _BINDING_RE.sub("", line).strip()
        if not _MD_TABLE_ROW_RE.match(s) or _MD_TABLE_SEP_RE.match(s):
            continue
        rows.append([c.strip() for c in s.strip("|").split("|")])
    return rows


#: 有序列表的序号：行首的 `1. ` / `2、` / `3) `。
_ORDERED_ITEM_RE = re.compile(r"^([\s*＊>]*)\d{1,2}\s*[.、)）]\s", re.MULTILINE)


def _strip_ordered_markers(sentence: str) -> str:
    """去掉有序列表的序号，它是排版而不是断言。

    实测（第二十轮）：模型把抗性推导写成 1./2./3./4./5. 五步，其中**只有第 3 步**被拦，
    理由是「数值 3 未出现在绑定证据中」——而 1、2、4、5 只是碰巧在证据原文里出现过。
    也就是说那条拦截与内容无关，纯看运气。

    **按位置删除而不是按值豁免**：按值豁免会把正文里同一个数字也一起放过
    （「3. 冷却时间 3 秒」里的第二个 3 仍须溯源）。表格序号列那条老规则是按值豁免的，
    但它有「整列必须构成从 1 开始的连续整数」这道很紧的判据兜着。
    """
    return _ORDERED_ITEM_RE.sub(r"\1", sentence)


def _index_column_numbers(block: str) -> set[str]:
    """检出**序号列**里的数字，这些数字不参与回填校验。

    模型会给表格加一列 1/2/3……，那是排版而不是断言：它的含义不来自证据，
    要求它可溯源必然失败（实测「数值 3 未出现在绑定证据 E1 中」，那个 3 是行号）。

    判据刻意保守：只有当某一列在**全部数据行**上都是纯整数、且构成从 1 开始的
    连续序列时，才认定为序号列。物品等级 60、权重 1000 这类真实数值不会连成
    1,2,3,…，因此不会被误判放过。
    """
    rows = _table_rows(block)
    # rows[0] 是表头行（分隔行已被 `_table_rows` 丢掉），必须**排除**再判列。
    #
    # 这里原先直接把整个 rows 拿去判「全列是纯整数」，于是表头那一格
    # （`#`、`序号`、`T 级`）永远让 fullmatch 失败、整列被跳过——**这个函数从来
    # 没生效过**，序号列的行号一直在参与回填校验。实测：`| # | 词缀文本 |` 的表
    # 被判「数值 1, 2 未出现在绑定证据 E1 中」，而 1/2 就是行号。
    #
    # 更早的注释还写着「表头那一格不是数字，上面的 fullmatch 已排除整列，所以
    # values 全部来自数据行」——前半句是对的，后半句由此**不成立**。注释自洽地
    # 描述了一个不成立的结论，比没有注释更能误导人。
    if len(rows) < 3:  # 表头 + 至少两个数据行，才能判断是不是 1..n
        return set()
    data = rows[1:]
    width = min(len(r) for r in data)
    ignored: set[str] = set()
    for col in range(width):
        cells = [r[col] for r in data]
        if not all(re.fullmatch(r"\d{1,3}", c) for c in cells):
            continue
        values = [int(c) for c in cells]
        if values == list(range(1, len(values) + 1)):
            ignored.update(_norm_num(c) for c in cells)
    return ignored


def _claim_value_norm(value: Any) -> str:
    return re.sub(r"[\s。！？!?，,、；;：:]", "", str(value or "")).lower()


def _parse_bench_cost_claim(value: str) -> list[tuple[str, str]] | None:
    """完整消费费用字段；任何未被费用对或允许分隔符覆盖的文本都拒绝。"""
    matches = list(_BENCH_COST_PAIR.finditer(value))
    if not matches:
        return None
    out: list[tuple[str, str]] = []
    cursor = 0
    for index, match in enumerate(matches):
        gap = value[cursor:match.start()]
        if index == 0:
            if gap.strip():
                return None
        elif not re.fullmatch(r"\s*(?:\+|和|与|、|，|,)\s*", gap):
            return None
        out.append((_norm_num(match.group(1)), _claim_value_norm(match.group(2))))
        cursor = match.end()
    if value[cursor:].strip():
        return None
    return out


def _bench_catalog_scope_error(
    sentence: str,
    evidence: list[Evidence],
    kind: SentenceKind,
) -> str:
    """正向核对目录字段；任何未精确匹配的语义都 fail closed。"""
    normalized = sentence.strip()
    compact = _claim_value_norm(normalized)
    claim_data = [e.claim_data for e in evidence if e.claim_data]

    # Catalog v1 不包含“采用某选项”的建议依据；建议也不能借目录 Citation 解锁。
    if kind == "recommendation":
        return "制作建议或选项采用结论"

    if _BENCH_PURE_BOUNDARY.fullmatch(compact):
        return ""

    effect_match = _BENCH_EFFECT_CLAIM.fullmatch(normalized)
    if effect_match:
        claimed = _claim_value_norm(effect_match.group(1))
        effects = {
            _claim_value_norm(data.get("effect_text"))
            for data in claim_data
            if data.get("effect_text")
        }
        if claimed and claimed in effects:
            return ""
        return "与 Citation 不一致的效果原文"

    cost_match = _BENCH_COST_CLAIM.fullmatch(normalized)
    if cost_match:
        claimed_costs = _parse_bench_cost_claim(cost_match.group(1))
        expected_cost_lists = [
            [
                (_norm_num(str(cost.get("amount"))), _claim_value_norm(cost.get("currency")))
                for cost in (data.get("raw_costs") or [])
                if isinstance(cost, dict) and cost.get("amount") is not None
                and cost.get("currency")
            ]
            for data in claim_data
        ]
        if claimed_costs and any(
            claimed_costs == expected for expected in expected_cost_lists if expected
        ):
            return ""
        return "与 Citation 不一致的原始费用"

    item_classes_match = _BENCH_ITEM_CLASSES_CLAIM.fullmatch(normalized)
    if item_classes_match:
        claimed_classes = {
            _claim_value_norm(value)
            for value in re.split(
                r"(?:和|与|、|，|,|\+)", item_classes_match.group(1)
            )
            if _claim_value_norm(value)
        }
        expected_class_sets = [
            {
                _claim_value_norm(item_class)
                for item_class in (data.get("item_classes") or [])
                if item_class
            }
            for data in claim_data
        ]
        if claimed_classes and any(
            claimed_classes <= expected for expected in expected_class_sets if expected
        ):
            return ""
        return "与 Citation 不一致的 ItemClasses"

    unlock_match = _BENCH_UNLOCK_CLAIM.fullmatch(normalized)
    if unlock_match:
        claimed_unlock = _claim_value_norm(unlock_match.group(1))
        unlock_values = {
            _claim_value_norm(value)
            for data in claim_data
            for value in (data.get("unlock_text"), data.get("unlock_hint"))
            if value
        }
        if claimed_unlock and claimed_unlock in unlock_values:
            return ""
        return "与 Citation 不一致的解锁信息"

    if _BENCH_CATALOG_PRESENCE.fullmatch(normalized):
        return ""

    for claim_name, pattern in _BENCH_CATALOG_FORBIDDEN:
        if pattern.search(normalized):
            return claim_name
    return "目录未明确覆盖的事实语义"


def _metamod_matrix_scope_error(
    sentence: str,
    evidence: list[Evidence],
    kind: SentenceKind,
) -> str:
    """只按同一 Citation 的 operation + guard + 原字段做正向核对。"""
    normalized = sentence.strip()
    claim_data = [e.claim_data for e in evidence if e.claim_data]
    if kind == "recommendation":
        return "制作建议或操作采用结论"
    if _METAMOD_PURE_BOUNDARY.fullmatch(normalized):
        return ""

    result_match = _METAMOD_RESULT_CLAIM.fullmatch(normalized)
    if result_match:
        operation = result_match.group(1)
        guard = result_match.group(2)
        raw_value = result_match.group(3)
        if any(
            operation == str(data.get("operation_text") or "")
            and guard == str(data.get("guard_text") or "")
            and raw_value == str(data.get("raw_value") or "")
            for data in claim_data
        ):
            return ""
        return "与 Citation 不一致的 operation、guard 或原始结果"

    note_match = _METAMOD_NOTE_CLAIM.fullmatch(normalized)
    if note_match:
        operation = note_match.group(1)
        guard = note_match.group(2)
        note = note_match.group(3)
        if any(
            operation == str(data.get("operation_text") or "")
            and guard == str(data.get("guard_text") or "")
            and note == str(data.get("note_text") or "")
            and bool(data.get("note_text"))
            for data in claim_data
        ):
            return ""
        return "与 Citation 不一致的原始注释"

    presence_match = _METAMOD_PRESENCE_CLAIM.fullmatch(normalized)
    if presence_match:
        operation = presence_match.group(1)
        guard = presence_match.group(2)
        if any(
            operation == str(data.get("operation_text") or "")
            and guard == str(data.get("guard_text") or "")
            for data in claim_data
        ):
            return ""
        return "与 Citation 不一致的矩阵记录存在性"

    for claim_name, pattern in _METAMOD_FORBIDDEN:
        if pattern.search(normalized):
            return claim_name
    return "矩阵未明确覆盖的事实语义"


def _affix_pool_claims(
    evidence: list[Evidence], required: set[str]
) -> dict[str, dict[str, Any]] | None:
    """按 kind 取唯一证据，并核对三类证据的关联键。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in evidence:
        kind = str(item.claim_data.get("kind") or "")
        if kind:
            grouped.setdefault(kind, []).append(item.claim_data)
    if any(len(grouped.get(kind, [])) != 1 for kind in required):
        return None
    claims = {kind: grouped[kind][0] for kind in required}
    pob = claims.get("pob_base")
    mapping = claims.get("base_mapping")
    pool = claims.get("pool_odds")
    if pob and mapping and not (
        pob.get("base_id") == mapping.get("base_id")
        and pob.get("name_en") == mapping.get("name_en")
        and pob.get("armour_tag") == mapping.get("armour_tag")
        and pob.get("pob_url") == mapping.get("pob_url")
    ):
        return None
    if pob and mapping and pool and not (
        pool.get("base_id") == pob.get("base_id")
        and pool.get("base_name_en") == pob.get("name_en")
        and pool.get("base_name_zh") == mapping.get("name_zh")
        and pool.get("armour_tag") == pob.get("armour_tag")
        and pool.get("modifier_page") == mapping.get("modifier_page")
        and pool.get("pob_url") == pob.get("pob_url")
        and pool.get("poedb_base_url") == mapping.get("poedb_url")
    ):
        return None
    return claims


def _affix_pool_scope_error(
    sentence: str,
    evidence: list[Evidence],
    kind: SentenceKind,
) -> str:
    """Affix Pool 精确模式只允许固定句式，并强制跨源证据共同使用。"""
    normalized = sentence.strip()
    if kind == "recommendation":
        return "制作建议或路线采用结论"

    if _AFFIX_POOL_PURE_BOUNDARY.fullmatch(normalized):
        if _affix_pool_claims(evidence, {"pool_odds"}) is not None:
            return ""
        return "边界声明缺少 POEDB 池证据"

    base_match = _AFFIX_POOL_BASE_CLAIM.fullmatch(normalized)
    if base_match:
        claims = _affix_pool_claims(evidence, {"pob_base", "base_mapping"})
        if claims is None:
            return "底材映射句必须同时绑定一致的 PoB 底材与 POEDB 桥接证据"
        name_zh, name_en, modifier_page = base_match.groups()
        pob = claims["pob_base"]
        mapping = claims["base_mapping"]
        if (
            name_zh == str(mapping.get("name_zh") or "")
            and name_en == str(pob.get("name_en") or "")
            and modifier_page == str(mapping.get("modifier_page") or "")
        ):
            return ""
        return "与 Citation 不一致的底材名称或词缀页映射"

    weight_match = _AFFIX_POOL_WEIGHT_CLAIM.fullmatch(normalized)
    if weight_match:
        claims = _affix_pool_claims(
            evidence, {"pob_base", "base_mapping", "pool_odds"}
        )
        if claims is None:
            return "权重概率句必须同时绑定一致的 PoB、桥接与 POEDB 池证据"
        (
            item_level,
            side_zh,
            total_weight,
            target_group,
            target_weight,
            numerator,
            denominator,
            decimal_value,
        ) = weight_match.groups()
        data = claims["pool_odds"]
        if (
            item_level == str(data.get("item_level"))
            and side_zh == str(data.get("generation_side_zh") or "")
            and total_weight == str(data.get("total_weight"))
            and target_group == str(data.get("target_group") or "")
            and target_weight == str(data.get("target_weight"))
            and numerator == str(data.get("probability_numerator"))
            and denominator == str(data.get("probability_denominator"))
            and decimal_value == str(data.get("probability_decimal") or "")
            and data.get("source_category") == "normal"
            and data.get("existing_family_exclusions_applied") is False
        ):
            return ""
        return "与 Citation 不一致的池条件、权重或条件概率"

    expected_match = _AFFIX_POOL_EXPECTED_CLAIM.fullmatch(normalized)
    if expected_match:
        claims = _affix_pool_claims(
            evidence, {"pob_base", "base_mapping", "pool_odds"}
        )
        if claims is None:
            return "数学期望句必须同时绑定一致的 PoB、桥接与 POEDB 池证据"
        target_group, expected = expected_match.groups()
        data = claims["pool_odds"]
        if (
            target_group == str(data.get("target_group") or "")
            and expected == str(data.get("expected_independent_selections") or "")
        ):
            return ""
        return "与 Citation 不一致的独立选择数学期望"

    for claim_name, pattern in _AFFIX_POOL_FORBIDDEN:
        if pattern.search(normalized):
            return claim_name
    return "条件权重工具未明确覆盖的事实语义"


def inheritable_labels(
    previous: SentenceVerdict | None, sentence: str
) -> tuple[str, ...]:
    """表格块可以继承**紧邻前一句**的绑定编号。返回空元组表示不继承。

    为什么只给表格开这个口子：模型的自然写法是「腰带上的力量词缀档位如下[[E1]]：」
    紧跟一张表。引导句绑定了证据、表格是它的续写，但分句之后表格成了独立单位、
    自己没有标记，于是**整张表被判「事实句未绑定引用」**。实测 104 句拦截里有 17 句
    是这种形态，其中 13 句紧邻前一句有绑定可继承。

    为什么这样做仍然安全：继承只提供**编号**，数值回填校验照旧跑——表里出现的每个
    数值仍必须能在继承来的证据原文里找到。实测那 13 句继承后**全部通过**数值校验
    （0 句需要继续拦），说明它们的数字本来就在证据里，拦它们是错误拒答。
    反过来，若模型在表里编造数字，继承编号也救不了它。

    三条限制：
      只有表格块能继承（表格是引导句的续写，普通句子不是）
      只继承**紧邻**且**已通过校验**的那一句（被拦的句子不能当依据来源）
      表格自己有标记时不继承（以它自己写的为准）
    """
    if previous is None or not previous.ok or not previous.labels:
        return ()
    if not _is_table_line(sentence):
        return ()
    if extract_labels(sentence):
        return ()
    return previous.labels


def _check_sentence(
    sentence: str,
    by_label: dict[str, Evidence],
    terms: tuple[str, ...],
    domain: tuple[str, ...] = (),
    inherited: tuple[str, ...] = (),
) -> SentenceVerdict:
    labels = extract_labels(sentence) or inherited
    stripped = _BINDING_RE.sub("", sentence)
    kind = classify(stripped, labels, terms, domain)
    affix_pool_mode = any(
        item.claim_scope == "affix_pool_odds" for item in by_label.values()
    )

    if affix_pool_mode and not labels:
        return _block(
            sentence, kind, labels,
            "条件池精确模式要求每句绑定固定范围证据；无引用叙述或建议一律拒绝",
            f"条件池精确回答出现未绑定句：{_excerpt(stripped)}",
        )
    if kind == "narrative" and not labels:
        return SentenceVerdict(sentence, kind, labels, True, "无需绑定")
    if kind == "recommendation" and not labels:
        # 纯建议不强制绑定；一旦主动绑定证据，仍须检查该证据的能力范围。
        return SentenceVerdict(sentence, kind, labels, True, "无需绑定")

    hedge = next((w for w in _HEDGE_WORDS if w in stripped), "")
    if hedge:
        return _block(
            sentence, kind, labels,
            f"含模糊限定语「{hedge}」。需求 1.5/15.1 禁止用它把推断包装成模糊表述",
            f"该点被模糊表述掩盖，需要确切依据：{_excerpt(stripped)}",
        )

    if not labels:
        return _block(
            sentence, kind, labels,
            "事实句未绑定引用",
            f"缺少可引用依据：{_excerpt(stripped)}",
        )

    unknown = [lb for lb in labels if lb not in by_label]
    if unknown:
        return _block(
            sentence, kind, labels,
            f"引用编号无效：{', '.join(unknown)}（可用 {', '.join(by_label) or '无'}）",
            f"引用编号无效，依据无法核实：{_excerpt(stripped)}",
        )

    bound = [by_label[lb] for lb in labels]
    restricted = [e for e in bound if e.claim_scope == "bench_catalog"]
    if restricted:
        scope_error = _bench_catalog_scope_error(stripped, restricted, kind)
        if scope_error:
            return _block(
                sentence, kind, labels,
                f"工艺台目录证据仅支撑页面目录字段，不能支撑{scope_error}",
                f"缺少可支撑{scope_error}的非目录证据：{_excerpt(stripped)}",
            )

    metamod_restricted = [e for e in bound if e.claim_scope == "metamod_matrix"]
    if metamod_restricted:
        marker_matches = list(_BINDING_RE.finditer(sentence))
        marker_tail = sentence[marker_matches[0].start():] if marker_matches else ""
        marker_tail_text = _BINDING_RE.sub("", marker_tail).strip(" \t\r\n。！？!?")
        if marker_tail_text:
            scope_error = "插入原字段内部的 Citation marker"
        else:
            # Metamod 的 operation / guard / raw_value / note 必须保留逐字原貌；
            # Citation 只能出现在声明末尾，不能靠全局删除 marker 拼接出合法字段。
            scope_error = _metamod_matrix_scope_error(
                _BINDING_RE.sub("", sentence), metamod_restricted, kind
            )
        if scope_error:
            return _block(
                sentence, kind, labels,
                f"Metamod 矩阵证据仅支撑页面单元格原字段，不能支撑{scope_error}",
                f"缺少可支撑{scope_error}的非矩阵证据：{_excerpt(stripped)}",
            )

    pool_restricted = [e for e in bound if e.claim_scope == "affix_pool_odds"]
    if pool_restricted:
        marker_matches = list(_BINDING_RE.finditer(sentence))
        marker_tail = sentence[marker_matches[0].start():] if marker_matches else ""
        marker_tail_text = _BINDING_RE.sub("", marker_tail).strip(" \t\r\n。！？!?")
        if marker_tail_text:
            scope_error = "插入原字段内部的 Citation marker"
        else:
            scope_error = _affix_pool_scope_error(
                _BINDING_RE.sub("", sentence), pool_restricted, kind
            )
        if scope_error:
            return _block(
                sentence, kind, labels,
                f"条件权重证据仅支撑窄化池模型原字段，不能支撑{scope_error}",
                f"缺少可支撑{scope_error}的非条件池证据：{_excerpt(stripped)}",
            )

    weak = [e for e in bound if not e.can_support_facts]
    if weak and len(weak) == len(bound):
        return _block(
            sentence, kind, labels,
            f"绑定的来源属社区级，不可支撑事实断言"
            f"（{weak[0].source}，P{weak[0].source_priority}）",
            f"仅有社区来源，未经权威来源证实：{_excerpt(stripped)}",
        )

    if kind in ("recommendation", "narrative"):
        return SentenceVerdict(sentence, kind, labels, True, "已通过证据范围校验")

    # 数值回填校验：句中数值必须能在绑定证据里找到。
    # 编造的具体形态就是数字，这一条抓得最准。
    # 有序列表的序号是排版，**按位置**去掉而不是按值豁免（见 `_strip_ordered_markers`）
    missing = unsupported_numbers(_strip_ordered_markers(stripped), bound)
    if missing and _is_table_line(sentence.strip("\n").splitlines()[0] if sentence.strip() else ""):
        # 表格的序号列是排版，不是断言（见 `_index_column_numbers`）
        ignored = _index_column_numbers(sentence)
        if ignored:
            missing = [m for m in missing if _norm_num(m) not in ignored]

    if kind == "derived":
        # 计算派生（需求 15.3、10）：结论数值本来就不在证据里——那是算出来的，
        # 而输入往往在**上一句**。所以不能要求同句内有可溯源数值。
        #
        # 但「因此」二字不能成为绕过闸门的后门：`verify` 的第二遍会检查整个回答里
        # 是否存在已通过校验的事实句作为推导输入，没有就把派生句一并拦掉。
        #
        # 待办（任务 3.5）：`calc/` 就位后派生数值应由计算器产出并回填校验，
        # 届时这里改为强校验，不再依赖「有事实句垫底」这种间接约束。
        return SentenceVerdict(
            sentence, kind, labels, True,
            "派生" + (f"，未溯源数值 {', '.join(missing)}" if missing else ""),
            missing_point="",
        )

    if missing:
        return _block(
            sentence, kind, labels,
            f"数值 {', '.join(missing)} 未出现在绑定证据 {', '.join(labels)} 中",
            f"数值 {', '.join(missing)} 无法溯源：{_excerpt(stripped)}",
        )

    return SentenceVerdict(sentence, kind, labels, True, "通过")


def _enforce_affix_pool_complete_answer(
    result: BindingResult,
    by_label: dict[str, Evidence],
) -> None:
    """精确池回答必须实际使用 P2 底材、跨源映射和 P1 池三类证据。"""
    required_kinds = {"pob_base", "base_mapping", "pool_odds"}
    labels_by_kind = {
        str(evidence.claim_data.get("kind") or ""): label
        for label, evidence in by_label.items()
        if evidence.claim_scope == "affix_pool_odds"
    }
    if not required_kinds <= set(labels_by_kind):
        return
    used = set(result.used_labels)
    missing = [labels_by_kind[kind] for kind in sorted(required_kinds) if labels_by_kind[kind] not in used]
    if not missing:
        return
    reason = f"条件池 exact 回答未实际使用全部三类证据：缺少 {', '.join(missing)}"
    if len(result.verdicts) == 1 and result.verdicts[0].reason == reason:
        return
    blocked = _block(
        "".join(verdict.text for verdict in result.verdicts),
        "factual",
        (),
        reason,
        "条件池 exact 结论缺少 PoB 底材、跨源映射或 POEDB 池证据的完整绑定",
    )
    result.verdicts = [blocked]
    result.blocked = 1
    result.gaps = [blocked.missing_point]
    result.used_labels = []


def _block(
    sentence: str, kind: SentenceKind, labels: tuple[str, ...],
    reason: str, missing_point: str,
) -> SentenceVerdict:
    return SentenceVerdict(
        text=sentence,
        kind=kind,
        labels=labels,
        ok=False,
        reason=reason,
        replacement=f"〔此处原有一条无据断言，已被拦截：{reason}〕",
        missing_point=missing_point,
    )


def extract_labels(sentence: str) -> tuple[str, ...]:
    out: list[str] = []
    for m in _BINDING_RE.finditer(sentence):
        for num in _LABEL_RE.finditer(m.group(1)):
            lb = f"E{num.group(1)}"
            if lb not in out:
                out.append(lb)
    return tuple(out)


def classify(
    sentence: str,
    labels: tuple[str, ...],
    terms: tuple[str, ...],
    domain_terms: tuple[str, ...] = (),
) -> SentenceKind:
    """规则优先的事实句判定。**默认按事实句处理。**

    顺序有讲究：建议先判，因为「建议把抗性堆到 75%」既含建议词也含数值与领域词，
    应归建议（附假设即可），不该被当成事实断言强制绑定。

    默认值是 `factual` 而不是 `narrative`——这是实测改过来的。早先判不出类型就
    当过渡句放行，结果「猎首还会让你免疫冰冻」这种既无数值也无领域词的编造机制句
    直接漏出。那正是本闸门最该拦的东西。所以过渡句改成**白名单**：只有匹配纯元话语
    句式的才放行，其余一律按事实句要求绑定引用。

    代价是像「另外还有一点。」这类无害的连接句也会被要求绑定。在「漏出无据断言」与
    「多拦一句过渡语」之间，本项目明确选后者（输出错误内容的代价高于输出查不到）。
    """
    s = sentence.strip()
    if not s:
        return "narrative"
    # Markdown 排版优先判：它比领域词命中更具体。
    # 标题里出现「猎首」不代表这句在断言什么，只是个小节标签。
    if is_structural(s):
        return "narrative"
    has_hedge = any(w in s for w in _HEDGE_WORDS)
    # 缺据声明：断言的是信息缺失，无法也不该绑定证据。
    # 但必须同时不含模糊限定语——否则「证据里没写，一般来说是 100」会整句蒙过去。
    if _DISCLOSURE_RE.search(_EMPHASIS_RE.sub("", s)) and not has_hedge:
        return "narrative"
    # 面向用户的操作指引。要求不含数字：带数字的是建议或事实，不是指引。
    if _META_INSTRUCTION_RE.search(s) and not has_hedge and not _NUM_RE.search(s):
        return "narrative"
    if any(h in s for h in _RECOMMEND_HINTS):
        return "recommendation"
    if any(h in s for h in _DERIVED_HINTS) and _NUM_RE.search(s):
        return "derived"
    if labels:
        # 模型自己标了引用，说明它认为这是事实句 —— 按事实句校验
        return "factual"
    # 引导句必须在领域词判定**之前**：`显式词缀如下：` 里的「词缀」是 factual_terms
    # 成员，按领域词判就成了事实句、被要求绑定引用，而它一个断言都没有。
    # 这类引导句在本领域的答案里极常见，拦它会把答案结构撕碎（与 Markdown 标题同理）。
    if not _NUM_RE.search(s) and (
        _COLON_LEAD_IN_RE.match(s) or _FRAMED_LEAD_IN_RE.match(s)
    ):
        return "narrative"
    if any(t in s for t in terms) or any(t in s for t in domain_terms):
        return "factual"
    if _NUM_RE.search(s):
        return "factual"
    if _NARRATIVE_RE.match(s):
        return "narrative"
    return "factual"


#: 有实义内容：至少含一个中日韩字符、拉丁字母或数字。
#: 只剩标点的碎片（分句时被切散的孤立 `）`）不承载断言。
_HAS_CONTENT_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ffA-Za-z0-9]")

#: \u6807\u9898/\u5217\u8868\u9879\u5f00\u5934\u7684**\u5e8f\u53f7**\uff1a`### 1. \u88c5\u5907\u8bcd\u7f00`\u3001`2\u3001\u6309\u6765\u6e90`\u3002
#: \u4e0e\u8868\u683c\u7684\u5e8f\u53f7\u5217\u540c\u4e00\u65cf\u2014\u2014\u5b83\u662f\u6392\u7248\u800c\u4e0d\u662f\u65ad\u8a00\u6570\u503c\uff0c\u5265\u6389\u4e4b\u540e\u518d\u5224\u300c\u6807\u9898\u91cc\u6709\u6ca1\u6709\u6570\u503c\u300d\u3002
#: \u4e0d\u5265\u7684\u8bdd `### 1. \u88c5\u5907\u8bcd\u7f00` \u4f1a\u56e0\u4e3a\u90a3\u4e2a 1 \u8d70\u4e8b\u5b9e\u53e5\u6821\u9a8c\u5e76\u88ab\u8981\u6c42\u7ed1\u5b9a\u5f15\u7528\uff08\u5b9e\u6d4b\u7684\u9519\u8bef\u62d2\u7b54\uff09\u3002
_LEADING_ORDINAL_RE = re.compile(r"^\s*\d{1,3}\s*[.\u3001)\uff09:\uff1a]\s*")

#: \u6574\u884c\u53ea\u6709\u4e00\u4e2a URL\u3002\u4e0d\u627f\u8f7d\u65ad\u8a00\uff0c\u8981\u6c42\u5b83\u7ed1\u5b9a\u5f15\u7528\u6ca1\u6709\u610f\u4e49\u3002
_BARE_URL_RE = re.compile(r"^\s*[<\uff08(\[]?\s*https?://\S+?\s*[>\uff09)\]]?\s*$")


def is_structural(sentence: str) -> bool:
    """是否为 Markdown 排版元素（标题 / 加粗标签 / 分隔线 / 空列表符号）
    或无实义内容的碎片。

    带数字的标题一律返回 False，交回事实句校验——数值是编造的主要载体。
    """
    s = sentence.strip()
    if not s:
        return True
    if not _HAS_CONTENT_RE.search(s):
        # 纯标点碎片。实测括号内含句号时会被切出一个孤立的「）」，
        # 按事实句处理就会在答案里插一条莫名的拦截说明。
        return True
    if _MD_RULE_RE.match(s) or _MD_BULLET_ONLY_RE.match(s):
        return True
    if _BARE_URL_RE.match(s):
        return True
    for pattern in (_MD_HEADING_RE, _MD_LABEL_RE):
        m = pattern.match(s)
        # 先剥前导序号再判数值：`### 1. 装备词缀` 的 1 是章节号不是断言，
        # 而 `## 生命上限 1000` 的 1000 必须继续走事实句校验。
        if m and not _NUM_RE.search(_LEADING_ORDINAL_RE.sub("", m.group(1))):
            return True
    return False


def unsupported_numbers(sentence: str, bound: list[Evidence]) -> list[str]:
    """找出句中无法在绑定证据里溯源的数值。

    证据里的区间 `(40-55)` 覆盖句中的具体值 `47`：模型从区间里取一个具体数
    是合理表述，不该判为编造。所以除了逐字匹配，还要做区间包含判断。
    """
    pool: set[str] = set()
    ranges: list[tuple[float, float]] = []
    for e in bound:
        pool |= e.numbers
        ranges.extend(_ranges_in(normalize_signs(e.text)))

    missing: list[str] = []
    for m in _NUM_RE.finditer(normalize_signs(sentence)):
        raw = m.group()
        norm = _norm_num(raw)
        if norm in pool:
            continue
        val = _to_float(raw)
        if val is not None and any(lo <= val <= hi for lo, hi in ranges):
            continue
        if norm not in missing:
            missing.append(raw)
    return missing


_RANGE_IN_TEXT_RE = re.compile(
    r"\(\s*([-+]?\d+(?:\.\d+)?)\s*[-—–~]\s*([-+]?\d+(?:\.\d+)?)\s*\)"
)


def _ranges_in(text: str) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for m in _RANGE_IN_TEXT_RE.finditer(text):
        a, b = _to_float(m.group(1)), _to_float(m.group(2))
        if a is not None and b is not None:
            out.append((min(a, b), max(a, b)))
    return out


def _norm_num(raw: str) -> str:
    """归一数值文本，使 `1,000`、`1000`、`1000.0` 视为同一个数。"""
    val = _to_float(raw)
    if val is None:
        return raw
    return str(int(val)) if float(val).is_integer() else str(val)


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", "").lstrip("+"))
    except (TypeError, ValueError):
        return None


def _excerpt(sentence: str, limit: int = 60) -> str:
    s = sentence.strip()
    return s if len(s) <= limit else s[:limit] + "…"
