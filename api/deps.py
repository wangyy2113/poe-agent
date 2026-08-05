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

import logging

from fastapi import Header, HTTPException

from core.config import get_secrets

logger = logging.getLogger(__name__)


def _bearer(authorization: str) -> str:
    return authorization.removeprefix("Bearer ").strip()


def user_keys() -> list[str]:
    try:
        raw = get_secrets().api_keys
    except Exception:  # noqa: BLE001  配置异常不应让鉴权变成放行
        return []
    return [k.strip() for k in raw.split(",") if k.strip()]


def require_user(authorization: str = Header(default="")) -> str:
    """普通接口鉴权。返回调用方标识（脱敏后），用于按 Key 限流与轨迹归属。"""
    keys = user_keys()
    if not keys:
        return "anonymous"
    supplied = _bearer(authorization)
    if supplied not in keys:
        raise HTTPException(status_code=401, detail="API Key 无效")
    return f"{supplied[:7]}…{supplied[-4:]}" if len(supplied) > 14 else "key"


def require_admin(authorization: str = Header(default="")) -> None:
    """管理端点鉴权。ADMIN_TOKEN 未配置时拒绝全部请求。"""
    token = ""
    try:
        token = get_secrets().admin_token.strip()
    except Exception:  # noqa: BLE001
        pass
    if not token:
        raise HTTPException(status_code=403, detail="ADMIN_TOKEN 未配置，管理端点已禁用")
    if _bearer(authorization) != token:
        raise HTTPException(status_code=401, detail="管理凭证无效")
