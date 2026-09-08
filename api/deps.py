"""路由层共享依赖：鉴权。

两套凭证严格分离（需求 5.3、16.9）：
  - `API_KEYS`   普通用户，用于 `/chat` 等业务接口
  - `ADMIN_TOKEN` 管理端点，`/admin`、`/index` 会触发外部抓取与索引重建

未配置时的行为**故意不对称**：
  - `ADMIN_TOKEN` 未配置 → 拒绝全部请求。没配就放行等于把高开销操作公开。
  - `API_KEYS` 未配置 → 放行。本机开发默认无需鉴权；对外提供服务时必须配置，
    否则会被当成免费 LLM 代理。启动日志里会就此告警。
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

from fastapi import Header, HTTPException, Request

from core.config import get_config, get_secrets

logger = logging.getLogger(__name__)


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class _TokenBucketLimiter:
    """单进程线程安全令牌桶；项目本身约束为单实例、单进程运行。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def retry_after(
        self,
        identity: str,
        *,
        requests_per_minute: float,
        burst: int,
    ) -> int:
        """消费一个令牌；成功返回 0，被限流则返回应等待的整秒数。"""
        now = self._clock()
        capacity = float(max(1, burst))
        refill_per_second = requests_per_minute / 60.0
        with self._lock:
            previous = self._buckets.get(identity)
            if previous is None:
                tokens = capacity
            else:
                elapsed = max(0.0, now - previous.updated_at)
                tokens = min(capacity, previous.tokens + elapsed * refill_per_second)
            if tokens >= 1.0:
                self._buckets[identity] = _Bucket(tokens=tokens - 1.0, updated_at=now)
                return 0
            self._buckets[identity] = _Bucket(tokens=tokens, updated_at=now)
            return max(1, math.ceil((1.0 - tokens) / refill_per_second))


_user_limiter = _TokenBucketLimiter()


def _bearer(authorization: str) -> str:
    return authorization.removeprefix("Bearer ").strip()


def user_keys() -> list[str]:
    try:
        raw = get_secrets().api_keys
    except Exception as exc:  # noqa: BLE001  配置异常必须失败关闭，不能冒充未配置
        logger.error("API 鉴权配置不可用：%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="API 鉴权配置不可用") from exc
    return [k.strip() for k in raw.split(",") if k.strip()]


def _rate_limit_settings() -> tuple[float, int] | None:
    cfg = get_config()
    if not bool(cfg.get("server.rate_limit.enabled", True)):
        return None
    try:
        requests_per_minute = float(
            cfg.get("server.rate_limit.requests_per_minute", 60)
        )
        burst = int(cfg.get("server.rate_limit.burst", 10))
    except (TypeError, ValueError):
        logger.error("server.rate_limit 配置无效，使用安全默认值 60 次/分钟、突发 10 次")
        return 60.0, 10
    if not math.isfinite(requests_per_minute) or requests_per_minute <= 0 or burst <= 0:
        logger.error("server.rate_limit 必须为正数，使用安全默认值 60 次/分钟、突发 10 次")
        return 60.0, 10
    return requests_per_minute, burst


def _enforce_user_rate_limit(identity: str) -> None:
    settings = _rate_limit_settings()
    if settings is None:
        return
    retry_after = _user_limiter.retry_after(
        identity,
        requests_per_minute=settings[0],
        burst=settings[1],
    )
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁，请稍后重试",
            headers={"Retry-After": str(retry_after)},
        )


def _admin_allowed_networks() -> tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network, ...
]:
    """解析管理端允许的真实连接地址；空列表表示只使用管理令牌鉴权。"""
    raw = get_config().get("server.admin_allowed_ips", [])
    if raw in (None, "", []):
        return ()
    if isinstance(raw, str):
        entries = [item.strip() for item in raw.split(",") if item.strip()]
    elif isinstance(raw, list):
        entries = [str(item).strip() for item in raw if str(item).strip()]
    else:
        logger.error("server.admin_allowed_ips 必须是字符串或列表")
        raise HTTPException(status_code=503, detail="管理端 IP 白名单配置无效")
    try:
        return tuple(ipaddress.ip_network(item, strict=False) for item in entries)
    except ValueError as exc:
        logger.error("server.admin_allowed_ips 含无效地址")
        raise HTTPException(status_code=503, detail="管理端 IP 白名单配置无效") from exc


def _require_admin_ip(request: Request) -> None:
    networks = _admin_allowed_networks()
    if not networks:
        return
    raw_host = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(raw_host)
    except ValueError:
        logger.warning("管理端拒绝无法识别的连接地址")
        raise HTTPException(status_code=403, detail="来源地址不在管理端白名单") from None
    # 一些 ASGI 服务器把 IPv4 peer 表示成 ::ffff:a.b.c.d；按真实 IPv4 与 CIDR 比较。
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if not any(address.version == network.version and address in network for network in networks):
        logger.warning("管理端拒绝非白名单连接地址：%s", address)
        raise HTTPException(status_code=403, detail="来源地址不在管理端白名单")


def require_user(authorization: str = Header(default="")) -> str:
    """普通接口鉴权。返回调用方标识（脱敏后），用于按 Key 限流与轨迹归属。"""
    keys = user_keys()
    if not keys:
        _enforce_user_rate_limit("anonymous")
        return "anonymous"
    supplied = _bearer(authorization)
    if supplied not in keys:
        raise HTTPException(status_code=401, detail="API Key 无效")
    # 桶键只留不可逆摘要，避免完整 API Key 常驻在限流器或调试快照里。
    identity = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
    _enforce_user_rate_limit(identity)
    return f"{supplied[:7]}…{supplied[-4:]}" if len(supplied) > 14 else "key"


def require_admin(request: Request, authorization: str = Header(default="")) -> None:
    """管理端点鉴权。ADMIN_TOKEN 未配置时拒绝全部请求。"""
    try:
        token = get_secrets().admin_token.strip()
    except Exception as exc:  # noqa: BLE001  配置读取失败不能降级成开放或伪装成未配置
        logger.error("管理鉴权配置不可用：%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="管理鉴权配置不可用") from exc
    if not token:
        raise HTTPException(status_code=403, detail="ADMIN_TOKEN 未配置，管理端点已禁用")
    _require_admin_ip(request)
    if _bearer(authorization) != token:
        raise HTTPException(status_code=401, detail="管理凭证无效")
