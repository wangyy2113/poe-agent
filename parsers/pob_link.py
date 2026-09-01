"""PoB 分享**链接** → 可下载的原文地址（第五十轮）。

上一轮 `/build` 只收分享码，链接明确留给这一轮，因为它引入两件新东西：
**外部抓取**与**该允许抓谁**。

## 站点表逐条抄自 PoB，不凭记忆

`Modules/BuildSiteTools.lua`（commit `9e6ea223`）里有一张权威表，
每个站点给出 `matchURL` / `regexURL` / `downloadURL`。照抄它的收益是具体的：
**pobb.in 的下载地址是 `pobb.in/pob/<id>` 而不是 `pobb.in/<id>/raw`**——
按「raw 结尾」这个印象写就是错的。这与第四十八轮从 `ImportTab.lua` 取编码方式
是同一条纪律：能钉住上游原文的，就不要凭印象。

## 白名单，不是黑名单

`kb/fetcher.reject_poe2_host()` 是**黑名单**（拒绝 POE2 数据站），那对采集入口合适——
要抓的站点本来就是我们自己挑的。**这里不一样**：URL 由用户提供，
服务端拿着它去发请求。放开等于让任何人指使本服务访问任意地址
（内网地址、云元数据端点都在射程内）。所以这里只认**这张表里的站点**，
其余一律拒绝——白名单的失败方向是「少支持一个站」，黑名单的失败方向是「多打一个内网」。

POE1/POE2 边界顺带落在同一处：poe.ninja 两款游戏都服务，而 PoB 的 POE1 规则
只匹配 `poe.ninja/poe1/pob/...` 与不带版本段的老路径；`poe2` 那一段匹配不上，
自然被这张表挡在外面。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

#: 表的出处。改这张表时对着这个 commit 的文件核，别照记忆改。
SOURCE = (
    "https://github.com/PathOfBuildingCommunity/PathOfBuilding/blob/"
    "9e6ea223e5921abd00bab36f246072e353140a58/src/Modules/BuildSiteTools.lua"
)


class PobLinkError(ValueError):
    """链接不被支持。**说清楚为什么**，并把支持的站点列出来。"""


@dataclass(frozen=True)
class BuildSite:
    label: str
    #: 从 URL 里取出 build id 的正则（由 PoB 的 `regexURL` 翻译成 Python）
    pattern: re.Pattern[str]
    #: 下载地址模板，`{id}` 占位（由 PoB 的 `downloadURL` 翻译而来）
    download: str


#: 逐条对应 `BuildSiteTools.lua` 的 `buildSites.websiteList`。
#: **顺序与那边一致**，方便逐行核对。
BUILD_SITES: tuple[BuildSite, ...] = (
    BuildSite("Maxroll", re.compile(r"^maxroll\.gg/poe/pob/(.+?)/?$"),
              "https://maxroll.gg/poe/api/pob/{id}"),
    BuildSite("pob.codes", re.compile(r"^pob\.codes/b/(.+?)/?$"),
              "https://api.pob.codes/{id}/raw"),
    # 注意是 `/pob/<id>`，不是 `/<id>/raw`——照记忆写会错
    BuildSite("pobb.in", re.compile(r"^pobb\.in/(.+?)/?$"),
              "https://pobb.in/pob/{id}"),
    # 只认 POE1 的路径；`poe.ninja/poe2/...` 匹配不上，POE2 边界落在这里
    BuildSite("poe.ninja", re.compile(r"^poe\.ninja/(?:poe1/)?pob/(\w+)/?$"),
              "https://poe.ninja/poe1/pob/raw/{id}"),
    BuildSite("Pastebin.com", re.compile(r"^pastebin\.com/(\w+)/?$"),
              "https://pastebin.com/raw/{id}"),
    BuildSite("PastebinP.com", re.compile(r"^pastebinp\.com/(\w+)/?$"),
              "https://pastebinp.com/raw/{id}"),
    BuildSite("Rentry.co", re.compile(r"^rentry\.co/(\w+)/?$"),
              "https://rentry.co/paste/{id}/raw"),
    BuildSite("poedb.tw", re.compile(r"^poedb\.tw/pob/(.+?)/?$"),
              "https://poedb.tw/pob/{id}/raw"),
)

SUPPORTED_LABELS = tuple(s.label for s in BUILD_SITES)


def looks_like_link(text: str) -> bool:
    """这串东西是链接还是分享码。分享码不含 `/` 与 `.`，链接必然有。"""
    head = (text or "").strip()
    return "://" in head or head.startswith("www.") or (
        "/" in head and "." in head.split("/", 1)[0]
    )


def resolve_download_url(link: str) -> str:
    """分享链接 → 原文下载地址。不在白名单里就抛 `PobLinkError`。"""
    raw = (link or "").strip()
    if not raw:
        raise PobLinkError("链接为空")
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.scheme not in ("http", "https"):
        raise PobLinkError(f"只支持 http/https，收到的是 {parsed.scheme!r}")
    host = (parsed.netloc or "").lower().split(":")[0].removeprefix("www.")
    # 归一成 `host/path` 再匹配——PoB 那张表的正则也是这么写的
    target = f"{host}{parsed.path}"
    for site in BUILD_SITES:
        matched = site.pattern.match(target)
        if matched:
            return site.download.format(id=matched.group(1))
    raise PobLinkError(
        f"不支持的站点 {host!r}。**只接受已知的 PoB 分享站**"
        f"（{'、'.join(SUPPORTED_LABELS)}）——"
        "服务端会拿着这个地址去发请求，放开就等于允许指使本服务访问任意地址。"
        "你也可以直接粘分享码本身。"
    )
