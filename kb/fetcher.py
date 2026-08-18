"""HTTP 采集基础设施：robots 校验 + 令牌桶限速 + 磁盘缓存 + 内容哈希增量。

合规（需求 2.8、16.8）：只抓公开页面、遵守 robots.txt、限速、缓存复用、
携带可识别 User-Agent。原始 HTML 落 data/（已 gitignore），不入版本库。

跨平台：缓存文件名用 URL 的哈希而非 URL 原文，避开 Windows MAX_PATH 260 限制。
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


class RateLimiter:
    """令牌桶。跨线程安全，按 requests_per_second 匀速放行。"""

    def __init__(self, rate_per_second: float) -> None:
        self.min_interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self.min_interval


class RobotsGate:
    """按域名缓存 robots.txt 判定结果。取不到 robots 时保守放行公开页面。"""

    def __init__(self, user_agent: str, timeout: float = 15.0) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        with self._lock:
            if origin not in self._cache:
                self._cache[origin] = self._load(origin)
            parser = self._cache[origin]
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    def _load(self, origin: str) -> urllib.robotparser.RobotFileParser | None:
        robots_url = f"{origin}/robots.txt"
        try:
            resp = httpx.get(
                robots_url,
                timeout=self.timeout,
                headers={"User-Agent": self.user_agent},
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            logger.warning("取 robots.txt 失败（保守放行）：%s -> %s", robots_url, exc)
            return None
        if resp.status_code >= 400:
            logger.info("robots.txt 返回 %d，视为未设限：%s", resp.status_code, robots_url)
            return None
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(resp.text.splitlines())
        logger.info("已加载 robots.txt：%s", robots_url)
        return parser


@dataclass
class FetchResult:
    #: 调用方请求的 URL；缓存键也由它派生。
    url: str
    text: str
    content_hash: str
    from_cache: bool
    fetched_at: str
    status_code: int = 200
    #: HTTP 跟随重定向后的最终 URL。缓存命中时仍使用原请求 URL，但上层必须
    #: 另外核对响应正文内的页面身份，不能只信缓存键。
    final_url: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300 and bool(self.text)


class Fetcher:
    """带缓存与限速的抓取器。

    缓存策略：
      - 命中且未过 TTL → 直接用缓存，不发请求（from_cache=True）
      - 过期或未命中 → 发请求并写缓存
      - content_hash 供上层判断内容是否变化，未变则跳过重新抽取与索引
    """

    def __init__(
        self,
        cache_dir: Path,
        user_agent: str,
        requests_per_second: float = 0.5,
        timeout: float = 60.0,
        cache_ttl_hours: int = 24,
        respect_robots: bool = True,
    ) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.user_agent = user_agent
        self.timeout = timeout
        self.cache_ttl = timedelta(hours=cache_ttl_hours)
        self.limiter = RateLimiter(requests_per_second)
        self.robots = RobotsGate(user_agent) if respect_robots else None

    def _cache_path(self, url: str) -> Path:
        return cache_path_for(self.cache_dir, url)

    def _cache_fresh(self, path: Path) -> bool:
        if not path.is_file():
            return False
        if self.cache_ttl.total_seconds() <= 0:
            return True
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        )
        return age < self.cache_ttl

    def fetch(self, url: str, *, force: bool = False) -> FetchResult:
        reject_poe2_host(url)
        cache_path = self._cache_path(url)

        if not force and self._cache_fresh(cache_path):
            text = cache_path.read_text(encoding="utf-8")
            logger.debug("缓存命中 %s", url)
            return FetchResult(
                url=url,
                text=text,
                content_hash=_sha256(text),
                from_cache=True,
                fetched_at=datetime.fromtimestamp(
                    cache_path.stat().st_mtime, tz=timezone.utc
                ).isoformat(timespec="seconds"),
                final_url=url,
            )

        if self.robots is not None and not self.robots.allowed(url):
            raise PermissionError(f"robots.txt 不允许抓取：{url}")

        self.limiter.acquire()
        logger.info("抓取 %s", url)
        resp = httpx.get(
            url,
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent, "Accept-Language": "zh-CN,zh;q=0.9"},
            follow_redirects=True,
        )
        if resp.status_code >= 400:
            return FetchResult(
                url=url,
                text="",
                content_hash="",
                from_cache=False,
                fetched_at=_now_iso(),
                status_code=resp.status_code,
                final_url=str(resp.url),
            )

        text = resp.text
        # 先写临时文件再替换，避免中断留下半个文件
        tmp = cache_path.with_suffix(".part")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(cache_path)

        return FetchResult(
            url=url,
            text=text,
            content_hash=_sha256(text),
            from_cache=False,
            fetched_at=_now_iso(),
            status_code=resp.status_code,
            final_url=str(resp.url),
        )


#: 《流放之路 2》的数据站与镜像。**本项目只覆盖 POE1**，这些域名的内容一旦进库，
#: 两款游戏的数值就混在同一个可引用索引里，而输出时看不出来。
#:
#: 放在抓取入口而不是各个抽取器里：抽取器有十几个，漏一个就等于没有护栏；
#: 而所有网络内容都经过 `Fetcher.fetch`。注意 `poedb.tw`（POE1）与 `poe2db.tw`（POE2）
#: 只差一个字符，配置里写错一个字母就会静默换游戏。
_POE2_HOSTS: tuple[str, ...] = ("poe2db.tw", "poe2wiki.net", "poe2.ninja")


class Poe2SourceRejected(PermissionError):
    """试图抓取 POE2 数据源。本项目只覆盖 POE1，这类内容不得进库。"""


def reject_poe2_host(url: str) -> None:
    """抓取入口的硬护栏：URL 指向 POE2 数据站时直接拒绝。"""
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    for bad in _POE2_HOSTS:
        if host == bad or host.endswith("." + bad):
            raise Poe2SourceRejected(
                f"拒绝抓取 POE2 数据源 {host}（{url}）——本项目只覆盖《流放之路 1》。"
                "两款游戏概念大量同名但数值不通用，混进同一个可引用索引后无法在输出里区分。"
            )


def cache_path_for(cache_dir: Path, url: str) -> Path:
    """URL → 缓存文件路径。

    公开函数而非只留 `Fetcher._cache_path`：覆盖层要判断「这一页是否已抓到本地」，
    如果它自己复制一遍哈希规则，两份规则迟早分叉，那时「已抓取」的判断就会失准。
    哈希文件名同时避开 Windows MAX_PATH 与非法字符问题。
    """
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    host = urlparse(url).netloc.replace(":", "_")
    return cache_dir / f"{host}_{digest[:24]}.html"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
